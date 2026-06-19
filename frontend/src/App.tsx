import { FormEvent, useEffect, useMemo, useState } from "react";

type Dialogue = {
  speaker: string;
  text: string;
  balloon: string;
  position: string;
  box: [number, number, number, number] | null;
  font_size: number;
  max_lines: number;
};

type Panel = {
  panel_id: string;
  bbox: [number, number, number, number];
  shot: string;
  camera: string;
  location_id: string;
  characters: string[];
  prompt: string;
  image_asset: string | null;
  image_candidates: ImageCandidate[];
  selected_candidate_id: string | null;
  dialogue: Dialogue[];
  sfx: { text: string; position: string; style: string }[];
  generation: {
    backend: string;
    prompt: string;
    negative_prompt: string;
    seed: number;
    workflow_id: string | null;
    prompt_id: string | null;
    width: number | null;
    height: number | null;
    fit_mode: "cover" | "contain";
    crop_anchor: "center" | "top" | "bottom" | "left" | "right";
    text_policy: "no_text";
    model_notes: string;
    status: string;
    message: string;
    loras: LoRABinding[];
    reference_images: ReferenceImageBinding[];
  };
};

type LoRABinding = { node_id: string; lora_name: string; strength_model: number; strength_clip: number };
type ReferenceImageBinding = { node_id: string; asset: string; character_id: string };

type ImageCandidate = {
  id: string;
  asset: string;
  backend: "stub" | "comfyui";
  status: "done" | "fallback" | "error";
  prompt: string;
  negative_prompt: string;
  characters: string[];
  loras: LoRABinding[];
  reference_images: ReferenceImageBinding[];
  seed: number;
  prompt_id: string | null;
  message: string;
  created_at: string;
};

type Character = {
  id: string;
  display_name: string;
  role: string;
  speech_style: string;
  visual_notes: string;
  trigger_prompt: string;
  appearance_prompt: string;
  outfit_prompt: string;
  negative_prompt: string;
  lora_node_id: string;
  lora_name: string;
  lora_strength_model: number;
  lora_strength_clip: number;
  reference_image_asset: string | null;
  reference_load_node_id: string;
};

type GenerationJob = {
  id: string;
  project_id: string;
  panel_id: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress: number;
  current: number;
  total: number;
  node: string | null;
  message: string;
  candidate_ids: string[];
};

type MangaProject = {
  title: string;
  work_name: string;
  premise: string;
  target_pages: number;
  common_positive_prompt: string;
  common_negative_prompt: string;
  characters: Character[];
  pages: { page: number; theme: string; layout_template: string; panels: Panel[]; render_status: "pending" | "done"; rendered_at: string | null }[];
};

type ProductionStatus = {
  project_id: string;
  status: "incomplete" | "ready" | "complete";
  adopted_panels: number;
  total_panels: number;
  rendered_pages: number;
  total_pages: number;
  pages: { page: number; status: "incomplete" | "ready" | "complete"; adopted_panels: number; total_panels: number; rendered: boolean; blockers: string[] }[];
  blockers: string[];
};

type Project = {
  id: string;
  title: string;
  work_name: string;
  manga_json: MangaProject;
};

type ProjectSummary = {
  id: string;
  title: string;
  work_name: string;
  updated_at: string;
};

type ComfyUIStatus = {
  backend: string;
  base_url: string;
  workflow_path: string;
  connected: boolean;
  workflow_exists: boolean;
  workflow_valid: boolean;
  missing_nodes: string[];
  message: string;
};

type TaskProgress = {
  label: string;
  current: number;
  total: number;
  indeterminate?: boolean;
};

const ANIME_PREVIEW_POSITIVE = "masterpiece, best quality, anime style, anime illustration, clean line art, vibrant colors";
const ANIME_PREVIEW_NEGATIVE = "low quality, worst quality, bad hands, bad anatomy, text, watermark, speech bubble, logo, extra fingers, missing fingers, distorted face";

const api = {
  async get<T>(path: string): Promise<T> {
    const response = await fetch(path);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async post<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async put<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
};

export function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selected, setSelected] = useState<Project | null>(null);
  const [selectedPage, setSelectedPage] = useState(1);
  const [jsonText, setJsonText] = useState("");
  const [pageAssets, setPageAssets] = useState<string[]>([]);
  const [selectedPanelId, setSelectedPanelId] = useState<string | null>(null);
  const [comfyStatus, setComfyStatus] = useState<ComfyUIStatus | null>(null);
  const [message, setMessage] = useState("準備完了");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [assetVersion, setAssetVersion] = useState(0);
  const [candidateCount, setCandidateCount] = useState(1);
  const [activeJobIds, setActiveJobIds] = useState<string[]>([]);
  const [productionStatus, setProductionStatus] = useState<ProductionStatus | null>(null);
  const [form, setForm] = useState({
    title: "テスト本",
    work_name: "サンプル作品",
    character_a: "キャラA",
    character_b: "キャラB",
    situation: "放課後の部室で差し入れを選ぶ",
    ending_direction: "小さな勘違いで笑って終わる"
  });

  const currentPage = useMemo(() => {
    return selected?.manga_json.pages.find((page) => page.page === selectedPage) ?? null;
  }, [selected, selectedPage]);

  const currentPanel = useMemo(() => {
    if (!currentPage) return null;
    return currentPage.panels.find((panel) => panel.panel_id === selectedPanelId) ?? currentPage.panels[0] ?? null;
  }, [currentPage, selectedPanelId]);

  const currentDialogue = currentPanel?.dialogue[0] ?? null;
  const effectivePrompts = useMemo(() => {
    if (!selected || !currentPanel) return { positive: "", negative: "" };
    return composePromptPreview(selected.manga_json, currentPanel);
  }, [selected, currentPanel]);

  useEffect(() => {
    void refreshProjects();
    void refreshComfyStatus();
  }, []);

  useEffect(() => {
    if (selected) void refreshProductionStatus(selected.id);
  }, [selected?.id]);

  useEffect(() => {
    if (currentPage?.panels.length && !currentPage.panels.some((panel) => panel.panel_id === selectedPanelId)) {
      setSelectedPanelId(currentPage.panels[0].panel_id);
    }
  }, [currentPage, selectedPanelId]);

  async function refreshProjects() {
    const list = await api.get<ProjectSummary[]>("/api/projects");
    setProjects(list);
  }

  async function refreshComfyStatus() {
    const status = await api.get<ComfyUIStatus>("/api/comfyui/status");
    setComfyStatus(status);
  }

  async function refreshProductionStatus(projectId: string) {
    const status = await api.get<ProductionStatus>(`/api/projects/${projectId}/production-status`);
    setProductionStatus(status);
  }

  async function runTask(task: () => Promise<void>) {
    setBusy(true);
    try {
      await task();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "処理に失敗しました");
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    await runTask(async () => {
      const project = await api.post<Project>("/api/projects", {
        title: form.title,
        work_name: form.work_name
      });
      setSelected(project);
      setSelectedPage(1);
      setSelectedPanelId(project.manga_json.pages[0]?.panels[0]?.panel_id ?? null);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets([]);
      await refreshProjects();
      setMessage("プロジェクトを作成しました");
    });
  }

  async function loadProject(id: string) {
    await runTask(async () => {
      const project = await api.get<Project>(`/api/projects/${id}`);
      setSelected(project);
      setSelectedPage(1);
      setSelectedPanelId(project.manga_json.pages[0]?.panels[0]?.panel_id ?? null);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets([]);
      setMessage("プロジェクトを読み込みました");
    });
  }

  async function generateName() {
    if (!selected) return;
    await runTask(async () => {
      const project = await api.post<Project>(`/api/projects/${selected.id}/generate-name`, {
        work_name: form.work_name,
        character_a: form.character_a,
        character_b: form.character_b,
        situation: form.situation,
        ending_direction: form.ending_direction
      });
      setSelected(project);
      setSelectedPage(1);
      setSelectedPanelId(project.manga_json.pages[0]?.panels[0]?.panel_id ?? null);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setMessage("4ページネームを生成しました");
      await refreshProjects();
      await refreshProductionStatus(project.id);
    });
  }

  async function saveJson() {
    if (!selected) return;
    await runTask(async () => {
      await saveJsonDraft("Manga JSONを保存しました");
      await refreshProductionStatus(selected.id);
    });
  }

  async function saveJsonDraft(successMessage: string): Promise<Project | null> {
    if (!selected) return null;
    const parsed = JSON.parse(jsonText) as MangaProject;
    const project = await api.put<Project>(`/api/projects/${selected.id}/manga-json`, parsed);
    setSelected(project);
    setJsonText(JSON.stringify(project.manga_json, null, 2));
    setMessage(successMessage);
    return project;
  }

  async function renderPages() {
    if (!selected) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("レンダリング前にManga JSONを保存しました");
      const projectId = saved?.id ?? selected.id;
      const manga = saved?.manga_json ?? selected.manga_json;
      const nextAssets = [...pageAssets];
      let latestManga = manga;
      for (const page of manga.pages) {
        const firstPanel = page.panels[0];
        if (!firstPanel) continue;
        setProgress({ label: `${page.page}ページをレンダリング中`, current: page.page, total: manga.pages.length });
        const response = await api.post<{ page_asset: string; manga_json: MangaProject }>(`/api/projects/${projectId}/panels/${firstPanel.panel_id}/render-page`);
        latestManga = response.manga_json;
        nextAssets[page.page - 1] = response.page_asset;
        setPageAssets([...nextAssets]);
        setAssetVersion((value) => value + 1);
      }
      const project = { ...(saved ?? selected), manga_json: latestManga };
      setSelected(project);
      setJsonText(JSON.stringify(latestManga, null, 2));
      setMessage("ページをレンダリングしました");
      await refreshProductionStatus(projectId);
    });
  }

  async function generateCurrentPanelImage() {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      setProgress({ label: "Manga JSONを保存中", current: 1, total: 4 });
      const saved = await saveJsonDraft("生成前にManga JSONを保存しました");
      const projectId = saved?.id ?? selected.id;
      const job = await createAndWaitForGenerationJob(projectId, currentPanel.panel_id);
      if (job.status !== "done") throw new Error(job.message);
      setProgress({ label: `${selectedPage}ページを更新中`, current: 3, total: 4 });
      const pageResponse = await api.post<{ page_asset: string; manga_json: MangaProject }>(`/api/projects/${projectId}/panels/${currentPanel.panel_id}/render-page`);
      const project = { ...(saved ?? selected), manga_json: pageResponse.manga_json };
      setSelected(project);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets((assets) => {
        const next = [...assets];
        next[selectedPage - 1] = pageResponse.page_asset;
        return next;
      });
      setAssetVersion((value) => value + 1);
      setProgress({ label: "プレビューを更新しました", current: 4, total: 4 });
      setMessage(`${currentPanel.panel_id}に候補を${candidateCount}件追加しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function generateCurrentPageImages() {
    if (!selected || !currentPage) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("一括生成前にManga JSONを保存しました");
      const projectId = saved?.id ?? selected.id;
      const panelIds = currentPage.panels.map((panel) => panel.panel_id);
      const batch = await api.post<{ jobs: GenerationJob[] }>(`/api/projects/${projectId}/generation-jobs`, {
        page: selectedPage,
        candidate_count: candidateCount
      });
      setActiveJobIds(batch.jobs.map((job) => job.id));
      try {
        for (const job of batch.jobs) {
          const completed = await watchGenerationJob(job);
          if (completed.status !== "done") throw new Error(completed.message);
        }
      } finally {
        setActiveJobIds([]);
      }
      let latestManga = (await api.get<Project>(`/api/projects/${projectId}`)).manga_json;
      const firstPanelId = panelIds[0];
      if (firstPanelId) {
        setProgress({ label: `${selectedPage}ページを更新中`, current: panelIds.length + 1, total: panelIds.length + 1 });
        const pageResponse = await api.post<{ page_asset: string; manga_json: MangaProject }>(`/api/projects/${projectId}/panels/${firstPanelId}/render-page`);
        latestManga = pageResponse.manga_json;
        setPageAssets((assets) => {
          const next = [...assets];
          next[selectedPage - 1] = pageResponse.page_asset;
          return next;
        });
      }
      setSelected({ ...(saved ?? selected), manga_json: latestManga });
      setJsonText(JSON.stringify(latestManga, null, 2));
      setAssetVersion((value) => value + 1);
      setMessage(`${selectedPage}ページの全コマを生成しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function createAndWaitForGenerationJob(projectId: string, panelId: string): Promise<GenerationJob> {
    const job = await api.post<GenerationJob>(
      `/api/projects/${projectId}/panels/${panelId}/generation-jobs`,
      { candidate_count: candidateCount }
    );
    setActiveJobIds([job.id]);
    try {
      return await watchGenerationJob(job);
    } finally {
      setActiveJobIds([]);
    }
  }

  function watchGenerationJob(initialJob: GenerationJob): Promise<GenerationJob> {
    return new Promise((resolve) => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${protocol}//${window.location.host}/api/generation-jobs/${initialJob.id}/ws`);
      let settled = false;
      let polling = false;
      const finish = (job: GenerationJob) => {
        if (settled) return;
        settled = true;
        socket.close();
        resolve(job);
      };
      const update = (job: GenerationJob) => {
        setProgress({
          label: job.node ? `${job.panel_id}: ${job.message} (${job.node})` : `${job.panel_id}: ${job.message}`,
          current: job.progress,
          total: 100,
          indeterminate: job.status === "queued" && job.progress === 0
        });
        if (["done", "error", "cancelled"].includes(job.status)) finish(job);
      };
      socket.onmessage = (event) => update(JSON.parse(event.data) as GenerationJob);
      const startPolling = () => {
        if (settled || polling) return;
        polling = true;
        socket.close();
        void pollGenerationJob(initialJob.id).then(finish);
      };
      socket.onerror = startPolling;
      socket.onclose = startPolling;
    });
  }

  async function pollGenerationJob(jobId: string): Promise<GenerationJob> {
    while (true) {
      const job = await api.get<GenerationJob>(`/api/generation-jobs/${jobId}`);
      setProgress({ label: job.message, current: job.progress, total: 100 });
      if (["done", "error", "cancelled"].includes(job.status)) return job;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }

  async function cancelActiveJob() {
    if (activeJobIds.length === 0) return;
    await Promise.all(activeJobIds.map((jobId) => api.post<GenerationJob>(`/api/generation-jobs/${jobId}/cancel`)));
    setMessage("生成キューをキャンセルしました");
  }

  async function selectCandidate(candidateId: string) {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      const response = await api.post<{ page_asset: string; manga_json: MangaProject }>(
        `/api/projects/${selected.id}/panels/${currentPanel.panel_id}/candidates/${candidateId}/select`
      );
      setSelected({ ...selected, manga_json: response.manga_json });
      setJsonText(JSON.stringify(response.manga_json, null, 2));
      setPageAssets((assets) => {
        const next = [...assets];
        next[selectedPage - 1] = response.page_asset;
        return next;
      });
      setAssetVersion((value) => value + 1);
      setMessage("画像候補を採用し、ページを更新しました");
      await refreshProductionStatus(selected.id);
    });
  }

  async function useStubForCurrentPanel() {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("stub生成前にManga JSONを保存しました");
      const projectId = saved?.id ?? selected.id;
      const response = await api.post<{ manga_json: MangaProject }>(`/api/projects/${projectId}/panels/${currentPanel.panel_id}/use-stub`);
      const pageResponse = await api.post<{ page_asset: string; manga_json: MangaProject }>(`/api/projects/${projectId}/panels/${currentPanel.panel_id}/render-page`);
      const project = { ...(saved ?? selected), manga_json: pageResponse.manga_json ?? response.manga_json };
      setSelected(project);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets((assets) => {
        const next = [...assets];
        next[selectedPage - 1] = pageResponse.page_asset;
        return next;
      });
      setAssetVersion((value) => value + 1);
      setMessage(`${currentPanel.panel_id}をstub画像へ戻しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function renderCurrentPanelPage() {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("写植更新前にManga JSONを保存しました");
      const projectId = saved?.id ?? selected.id;
      const response = await api.post<{ page_asset: string; manga_json: MangaProject }>(`/api/projects/${projectId}/panels/${currentPanel.panel_id}/render-page`);
      const project = { ...(saved ?? selected), manga_json: response.manga_json };
      setSelected(project);
      setJsonText(JSON.stringify(response.manga_json, null, 2));
      setPageAssets((assets) => {
        const next = [...assets];
        next[selectedPage - 1] = response.page_asset;
        return next;
      });
      setAssetVersion((value) => value + 1);
      setMessage(`${selectedPage}ページを更新しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function exportCbz() {
    if (!selected) return;
    await runTask(async () => {
      const response = await api.post<{ cbz_asset: string; warnings: string[] }>(`/api/projects/${selected.id}/export/cbz`);
      const warning = response.warnings.length ? ` / 警告 ${response.warnings.length}件` : "";
      setMessage(`CBZを書き出しました: /api/assets/${response.cbz_asset}${warning}`);
      await refreshProductionStatus(selected.id);
    });
  }

  function updateCurrentPanel(mutator: (panel: Panel) => void) {
    if (!selected || !currentPanel) return;
    const nextManga = structuredClone(selected.manga_json);
    for (const page of nextManga.pages) {
      const panel = page.panels.find((item) => item.panel_id === currentPanel.panel_id);
      if (panel) {
        mutator(panel);
        break;
      }
    }
    const nextProject = { ...selected, manga_json: nextManga };
    setSelected(nextProject);
    setJsonText(JSON.stringify(nextManga, null, 2));
  }

  function updateManga(mutator: (manga: MangaProject) => void) {
    if (!selected) return;
    const nextManga = structuredClone(selected.manga_json);
    mutator(nextManga);
    const nextProject = { ...selected, manga_json: nextManga };
    setSelected(nextProject);
    setJsonText(JSON.stringify(nextManga, null, 2));
  }

  function applyAnimePreviewDefaults() {
    updateManga((manga) => {
      manga.common_positive_prompt = ANIME_PREVIEW_POSITIVE;
      manga.common_negative_prompt = ANIME_PREVIEW_NEGATIVE;
    });
  }

  function applyFourPageDraftSettings() {
    const pageConfigs: Record<number, { prompt: string; width: number; height: number; fit: "cover" | "contain"; anchor: "center" | "top" | "bottom" | "left" | "right" }> = {
      1: { prompt: "establishing shot, after school room, soft daylight, calm mood", width: 1024, height: 640, fit: "cover", anchor: "center" },
      2: { prompt: "two character conversation, expressive faces, medium shot, clean background", width: 896, height: 640, fit: "cover", anchor: "center" },
      3: { prompt: "dynamic reaction, comedic timing, energetic pose, manga composition", width: 896, height: 672, fit: "cover", anchor: "top" },
      4: { prompt: "punchline scene, comedic contrast, clear silhouettes, final panel emphasis", width: 1024, height: 768, fit: "cover", anchor: "center" }
    };
    updateManga((manga) => {
      for (const page of manga.pages) {
        const config = pageConfigs[page.page];
        if (!config) continue;
        for (const panel of page.panels) {
          panel.generation.prompt = mergePrompt(config.prompt, panel.generation.prompt || panel.prompt);
          panel.generation.negative_prompt = manga.common_negative_prompt || ANIME_PREVIEW_NEGATIVE;
          panel.generation.width = config.width;
          panel.generation.height = config.height;
          panel.generation.fit_mode = config.fit;
          panel.generation.crop_anchor = config.anchor;
        }
      }
    });
    setMessage("4ページ分の仮設定を反映しました");
  }

  function updateCurrentDialogue(mutator: (dialogue: Dialogue) => void) {
    updateCurrentPanel((panel) => {
      if (panel.dialogue.length === 0) {
        panel.dialogue.push({
          speaker: panel.characters[0] ?? "char_a",
          text: "台詞",
          balloon: "round",
          position: "upper_right",
          box: [0.48, 0.06, 0.46, 0.22],
          font_size: 24,
          max_lines: 3
        });
      }
      mutator(panel.dialogue[0]);
    });
  }

  function updateCharacter(characterId: string, mutator: (character: Character) => void) {
    updateManga((manga) => {
      const character = manga.characters.find((item) => item.id === characterId);
      if (character) mutator(character);
    });
  }

  function addCharacter() {
    updateManga((manga) => {
      let index = manga.characters.length + 1;
      while (manga.characters.some((character) => character.id === `char_${index}`)) index += 1;
      manga.characters.push({
        id: `char_${index}`,
        display_name: `キャラ${index}`,
        role: "",
        speech_style: "",
        visual_notes: "",
        trigger_prompt: "",
        appearance_prompt: "",
        outfit_prompt: "",
        negative_prompt: "",
        lora_node_id: "",
        lora_name: "",
        lora_strength_model: 1,
        lora_strength_clip: 1,
        reference_image_asset: null,
        reference_load_node_id: ""
      });
    });
    setMessage("キャラクタープロファイルを追加しました");
  }

  function toggleCurrentPanelCharacter(characterId: string) {
    updateCurrentPanel((panel) => {
      panel.characters = panel.characters.includes(characterId)
        ? panel.characters.filter((id) => id !== characterId)
        : [...panel.characters, characterId];
    });
  }

  async function uploadReferenceImage(characterId: string, file: File) {
    if (!selected) return;
    await runTask(async () => {
      const response = await fetch(`/api/projects/${selected.id}/characters/${characterId}/reference-image`, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file
      });
      if (!response.ok) throw new Error(await response.text());
      const payload = await response.json() as { manga_json: MangaProject };
      setSelected({ ...selected, manga_json: payload.manga_json });
      setJsonText(JSON.stringify(payload.manga_json, null, 2));
      setAssetVersion((value) => value + 1);
      setMessage("参照画像を登録しました");
    });
  }

  function updateDialogueBox(index: number, value: number) {
    updateCurrentDialogue((dialogue) => {
      const box = dialogue.box ?? [0.48, 0.06, 0.46, 0.22];
      const next = [...box] as [number, number, number, number];
      next[index] = clamp(value, 0, 1);
      if (index === 0) next[0] = Math.min(next[0], 1 - next[2]);
      if (index === 1) next[1] = Math.min(next[1], 1 - next[3]);
      if (index === 2) next[2] = Math.min(Math.max(next[2], 0.05), 1 - next[0]);
      if (index === 3) next[3] = Math.min(Math.max(next[3], 0.05), 1 - next[1]);
      dialogue.box = next;
    });
  }

  function assetUrl(asset: string): string {
    const normalized = asset.replaceAll("\\", "/").replace(/^exports\//, "");
    return `/api/assets/${normalized}?v=${assetVersion}`;
  }

  function clamp(value: number, min: number, max: number): number {
    if (Number.isNaN(value)) return min;
    return Math.max(min, Math.min(max, value));
  }

  function mergePrompt(prefix: string, prompt: string): string {
    const cleanPrefix = prefix.trim();
    const cleanPrompt = prompt.trim();
    if (!cleanPrefix) return cleanPrompt;
    if (!cleanPrompt) return cleanPrefix;
    if (cleanPrompt.toLowerCase().includes(cleanPrefix.toLowerCase())) return cleanPrompt;
    return `${cleanPrefix}, ${cleanPrompt}`;
  }

  function composePromptPreview(manga: MangaProject, panel: Panel): { positive: string; negative: string } {
    const characterMap = new Map(manga.characters.map((character) => [character.id, character]));
    const positive = [manga.common_positive_prompt];
    const negative = [manga.common_negative_prompt];
    for (const characterId of panel.characters) {
      const character = characterMap.get(characterId);
      if (!character) continue;
      positive.push(character.trigger_prompt || character.display_name, character.appearance_prompt, character.outfit_prompt);
      negative.push(character.negative_prompt);
    }
    positive.push(panel.generation.prompt || panel.prompt);
    negative.push(panel.generation.negative_prompt);
    return { positive: mergePromptParts(positive), negative: mergePromptParts(negative) };
  }

  function mergePromptParts(parts: string[]): string {
    const seen = new Set<string>();
    const tags: string[] = [];
    for (const part of parts) {
      for (const rawTag of part.split(",")) {
        const tag = rawTag.trim();
        const key = tag.toLocaleLowerCase();
        if (tag && !seen.has(key)) {
          tags.push(tag);
          seen.add(key);
        }
      }
    }
    return tags.join(", ");
  }

  const progressPercent = progress ? Math.round((progress.current / Math.max(progress.total, 1)) * 100) : 0;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>Local Doujin Studio</h1>
        <form onSubmit={createProject} className="stack">
          <label>
            タイトル
            <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} />
          </label>
          <label>
            作品名
            <input value={form.work_name} onChange={(event) => setForm({ ...form, work_name: event.target.value })} />
          </label>
          <button disabled={busy}>新規作成</button>
        </form>
        <div className="project-list">
          <h2>プロジェクト</h2>
          {projects.map((project) => (
            <button key={project.id} className={selected?.id === project.id ? "selected" : ""} onClick={() => void loadProject(project.id)}>
              <span>{project.title}</span>
              <small>{project.work_name || "作品名未設定"}</small>
            </button>
          ))}
        </div>
      </aside>

      <main className="workspace">
        <header className="toolbar">
          <div>
            <strong>{selected?.title ?? "プロジェクト未選択"}</strong>
            <span>{message}</span>
          </div>
          <div className="actions">
            <button onClick={() => void refreshComfyStatus()} disabled={busy}>接続確認</button>
            <button onClick={generateName} disabled={!selected || busy}>ネーム生成</button>
            <button onClick={saveJson} disabled={!selected || busy}>JSON保存</button>
            <button onClick={renderPages} disabled={!selected || busy}>レンダリング</button>
            <button onClick={exportCbz} disabled={!selected || busy}>CBZ出力</button>
          </div>
        </header>

        {progress && (
          <section className="progress-band">
            <div>
              <strong>{progress.label}</strong>
              <span>{progress.current} / {progress.total}</span>
            </div>
            <progress className={progress.indeterminate ? "indeterminate" : ""} value={progress.indeterminate ? undefined : progressPercent} max="100" />
            {activeJobIds.length > 0 && <button onClick={cancelActiveJob}>キャンセル</button>}
          </section>
        )}

        <section className="status-band">
          <strong>{comfyStatus?.backend === "comfyui" ? "ComfyUI" : "stub"}</strong>
          <span>{comfyStatus?.message ?? "接続状態を確認中"}</span>
          <small>
            workflow: {comfyStatus?.workflow_exists ? "あり" : "なし"} / node: {comfyStatus?.workflow_valid ? "OK" : "未検証"}
          </small>
        </section>

        {productionStatus && (
          <section className={`production-band ${productionStatus.status}`}>
            <strong>{productionStatus.status === "complete" ? "制作完了" : productionStatus.status === "ready" ? "レンダリング待ち" : "制作中"}</strong>
            <span>採用 {productionStatus.adopted_panels}/{productionStatus.total_panels}コマ</span>
            <span>ページ {productionStatus.rendered_pages}/{productionStatus.total_pages}</span>
            {productionStatus.blockers.length > 0 && (
              <details>
                <summary>未完了 {productionStatus.blockers.length}件</summary>
                <ul>{productionStatus.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
              </details>
            )}
          </section>
        )}

        <section className="generator">
          <label>
            キャラA
            <input value={form.character_a} onChange={(event) => setForm({ ...form, character_a: event.target.value })} />
          </label>
          <label>
            キャラB
            <input value={form.character_b} onChange={(event) => setForm({ ...form, character_b: event.target.value })} />
          </label>
          <label>
            シチュエーション
            <input value={form.situation} onChange={(event) => setForm({ ...form, situation: event.target.value })} />
          </label>
          <label>
            オチの方向
            <input value={form.ending_direction} onChange={(event) => setForm({ ...form, ending_direction: event.target.value })} />
          </label>
        </section>

        {selected && (
          <section className="common-prompts">
            <label>
              全コマ共通positive
              <textarea
                value={selected.manga_json.common_positive_prompt}
                onChange={(event) => updateManga((manga) => {
                  manga.common_positive_prompt = event.target.value;
                })}
                spellCheck={false}
              />
            </label>
            <label>
              全コマ共通negative
              <textarea
                value={selected.manga_json.common_negative_prompt}
                onChange={(event) => updateManga((manga) => {
                  manga.common_negative_prompt = event.target.value;
                })}
                spellCheck={false}
              />
            </label>
            <div className="actions">
              <button onClick={applyAnimePreviewDefaults} disabled={busy}>anime-preview3-base向け初期値</button>
              <button onClick={applyFourPageDraftSettings} disabled={busy}>4ページ仮設定</button>
            </div>
          </section>
        )}

        {selected && (
          <section className="character-profiles">
            <div className="section-heading">
              <h2>キャラクタープロファイル</h2>
              <button onClick={addCharacter} disabled={busy}>キャラ追加</button>
            </div>
            <div className="character-profile-list">
              {selected.manga_json.characters.map((character) => (
                <article key={character.id}>
                  <div className="character-title">
                    <strong>{character.display_name}</strong>
                    <small>{character.id}</small>
                  </div>
                  <label>
                    表示名
                    <input
                      value={character.display_name}
                      onChange={(event) => updateCharacter(character.id, (item) => { item.display_name = event.target.value; })}
                    />
                  </label>
                  <label>
                    trigger prompt
                    <input
                      value={character.trigger_prompt}
                      onChange={(event) => updateCharacter(character.id, (item) => { item.trigger_prompt = event.target.value; })}
                    />
                  </label>
                  <label>
                    外見タグ
                    <textarea
                      value={character.appearance_prompt}
                      onChange={(event) => updateCharacter(character.id, (item) => { item.appearance_prompt = event.target.value; })}
                      spellCheck={false}
                    />
                  </label>
                  <label>
                    衣装タグ
                    <textarea
                      value={character.outfit_prompt}
                      onChange={(event) => updateCharacter(character.id, (item) => { item.outfit_prompt = event.target.value; })}
                      spellCheck={false}
                    />
                  </label>
                  <label>
                    個別negative
                    <input
                      value={character.negative_prompt}
                      onChange={(event) => updateCharacter(character.id, (item) => { item.negative_prompt = event.target.value; })}
                    />
                  </label>
                  <label>
                    LoRAノードID
                    <input value={character.lora_node_id} onChange={(event) => updateCharacter(character.id, (item) => { item.lora_node_id = event.target.value; })} />
                  </label>
                  <label>
                    LoRA名
                    <input value={character.lora_name} onChange={(event) => updateCharacter(character.id, (item) => { item.lora_name = event.target.value; })} />
                  </label>
                  <label>
                    model強度
                    <input type="number" step="0.05" min="-2" max="2" value={character.lora_strength_model} onChange={(event) => updateCharacter(character.id, (item) => { item.lora_strength_model = Number(event.target.value); })} />
                  </label>
                  <label>
                    CLIP強度
                    <input type="number" step="0.05" min="-2" max="2" value={character.lora_strength_clip} onChange={(event) => updateCharacter(character.id, (item) => { item.lora_strength_clip = Number(event.target.value); })} />
                  </label>
                  <label>
                    参照画像ノードID
                    <input value={character.reference_load_node_id} onChange={(event) => updateCharacter(character.id, (item) => { item.reference_load_node_id = event.target.value; })} />
                  </label>
                  <label>
                    参照画像
                    <input
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) void uploadReferenceImage(character.id, file);
                      }}
                    />
                  </label>
                  {character.reference_image_asset && (
                    <img className="reference-image" src={assetUrl(character.reference_image_asset)} alt={`${character.display_name}参照画像`} />
                  )}
                </article>
              ))}
            </div>
          </section>
        )}

        <section className="content-grid">
          <div className="preview">
            <div className="tabs">
              {[1, 2, 3, 4].map((page) => (
                <button key={page} className={selectedPage === page ? "active" : ""} onClick={() => setSelectedPage(page)}>
                  {page}p {productionStatus?.pages.find((item) => item.page === page)?.status === "complete" ? "完了" : ""}
                </button>
              ))}
            </div>
            <div className="page-frame">
              {pageAssets[selectedPage - 1] ? (
                <img src={assetUrl(pageAssets[selectedPage - 1])} alt={`${selectedPage}ページ`} />
              ) : (
                <div className="page-placeholder">
                  <strong>{currentPage ? `${currentPage.page}ページ` : "未生成"}</strong>
                  <span>{currentPage?.theme ?? "ネーム生成後にプレビューできます"}</span>
                </div>
              )}
            </div>
            <div className="panel-list">
              {currentPage?.panels.map((panel) => (
                <article
                  key={panel.panel_id}
                  className={currentPanel?.panel_id === panel.panel_id ? "active-panel" : ""}
                  onClick={() => setSelectedPanelId(panel.panel_id)}
                >
                  <strong>{panel.panel_id}</strong>
                  <span>{panel.shot}</span>
                  <small>{panel.generation.backend} / {panel.generation.status}</small>
                </article>
              ))}
            </div>
            {currentPanel && (
              <div className="panel-editor">
                <h2>選択中のコマ</h2>
                <div className="panel-meta">
                  <strong>{currentPanel.panel_id}</strong>
                  <span>{currentPanel.generation.message || "生成メッセージなし"}</span>
                  {currentPanel.generation.prompt_id && <small>prompt_id: {currentPanel.generation.prompt_id}</small>}
                </div>
                {selected && selected.manga_json.characters.length > 0 && (
                  <fieldset className="panel-characters">
                    <legend>登場キャラ</legend>
                    {selected.manga_json.characters.map((character) => (
                      <label key={character.id}>
                        <input
                          type="checkbox"
                          checked={currentPanel.characters.includes(character.id)}
                          onChange={() => toggleCurrentPanelCharacter(character.id)}
                        />
                        {character.display_name}
                      </label>
                    ))}
                  </fieldset>
                )}
                <label>
                  positive prompt
                  <textarea
                    value={currentPanel.generation.prompt || currentPanel.prompt}
                    onChange={(event) => updateCurrentPanel((panel) => {
                      panel.generation.prompt = event.target.value;
                    })}
                    spellCheck={false}
                  />
                </label>
                <label>
                  negative prompt
                  <input
                    value={currentPanel.generation.negative_prompt}
                    onChange={(event) => updateCurrentPanel((panel) => {
                      panel.generation.negative_prompt = event.target.value;
                    })}
                  />
                </label>
                <details className="prompt-preview">
                  <summary>実生成prompt</summary>
                  <label>
                    positive
                    <textarea value={effectivePrompts.positive} readOnly spellCheck={false} />
                  </label>
                  <label>
                    negative
                    <textarea value={effectivePrompts.negative} readOnly spellCheck={false} />
                  </label>
                </details>
                <label>
                  seed
                  <input
                    type="number"
                    value={currentPanel.generation.seed}
                    onChange={(event) => updateCurrentPanel((panel) => {
                      panel.generation.seed = Number(event.target.value);
                    })}
                  />
                </label>
                <div className="settings-grid">
                  <label>
                    生成幅
                    <input
                      type="number"
                      min={64}
                      max={4096}
                      value={currentPanel.generation.width ?? ""}
                      placeholder="workflow既定"
                      onChange={(event) => updateCurrentPanel((panel) => {
                        panel.generation.width = event.target.value ? Number(event.target.value) : null;
                      })}
                    />
                  </label>
                  <label>
                    生成高さ
                    <input
                      type="number"
                      min={64}
                      max={4096}
                      value={currentPanel.generation.height ?? ""}
                      placeholder="workflow既定"
                      onChange={(event) => updateCurrentPanel((panel) => {
                        panel.generation.height = event.target.value ? Number(event.target.value) : null;
                      })}
                    />
                  </label>
                  <label>
                    配置
                    <select
                      value={currentPanel.generation.fit_mode}
                      onChange={(event) => updateCurrentPanel((panel) => {
                        panel.generation.fit_mode = event.target.value as "cover" | "contain";
                      })}
                    >
                      <option value="cover">cover</option>
                      <option value="contain">contain</option>
                    </select>
                  </label>
                  <label>
                    crop基準
                    <select
                      value={currentPanel.generation.crop_anchor}
                      onChange={(event) => updateCurrentPanel((panel) => {
                        panel.generation.crop_anchor = event.target.value as "center" | "top" | "bottom" | "left" | "right";
                      })}
                    >
                      <option value="center">center</option>
                      <option value="top">top</option>
                      <option value="bottom">bottom</option>
                      <option value="left">left</option>
                      <option value="right">right</option>
                    </select>
                  </label>
                </div>
                <div className="dialogue-editor">
                  <h3>写植</h3>
                  <label>
                    台詞
                    <textarea
                      value={currentDialogue?.text ?? ""}
                      onChange={(event) => updateCurrentDialogue((dialogue) => {
                        dialogue.text = event.target.value;
                      })}
                    />
                  </label>
                  <div className="settings-grid">
                    <label>
                      x
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        max="1"
                        value={currentDialogue?.box?.[0] ?? 0.48}
                        onChange={(event) => updateDialogueBox(0, Number(event.target.value))}
                      />
                    </label>
                    <label>
                      y
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        max="1"
                        value={currentDialogue?.box?.[1] ?? 0.06}
                        onChange={(event) => updateDialogueBox(1, Number(event.target.value))}
                      />
                    </label>
                    <label>
                      幅
                      <input
                        type="number"
                        step="0.01"
                        min="0.05"
                        max="1"
                        value={currentDialogue?.box?.[2] ?? 0.46}
                        onChange={(event) => updateDialogueBox(2, Number(event.target.value))}
                      />
                    </label>
                    <label>
                      高さ
                      <input
                        type="number"
                        step="0.01"
                        min="0.05"
                        max="1"
                        value={currentDialogue?.box?.[3] ?? 0.22}
                        onChange={(event) => updateDialogueBox(3, Number(event.target.value))}
                      />
                    </label>
                    <label>
                      フォント
                      <input
                        type="number"
                        min="10"
                        max="72"
                        value={currentDialogue?.font_size ?? 24}
                        onChange={(event) => updateCurrentDialogue((dialogue) => {
                          dialogue.font_size = Number(event.target.value);
                        })}
                      />
                    </label>
                    <label>
                      最大行
                      <input
                        type="number"
                        min="1"
                        max="8"
                        value={currentDialogue?.max_lines ?? 3}
                        onChange={(event) => updateCurrentDialogue((dialogue) => {
                          dialogue.max_lines = Number(event.target.value);
                        })}
                      />
                    </label>
                  </div>
                </div>
                {currentPanel.image_asset && (
                  <img className="panel-image" src={assetUrl(currentPanel.image_asset)} alt={`${currentPanel.panel_id}画像`} />
                )}
                <div className="candidate-header">
                  <h3>画像候補</h3>
                  <label>
                    一度に生成
                    <select value={candidateCount} onChange={(event) => setCandidateCount(Number(event.target.value))} disabled={busy}>
                      {[1, 2, 3, 4].map((count) => <option key={count} value={count}>{count}件</option>)}
                    </select>
                  </label>
                </div>
                {currentPanel.image_candidates.length > 0 ? (
                  <div className="candidate-gallery">
                    {currentPanel.image_candidates.map((candidate) => (
                      <article key={candidate.id} className={currentPanel.selected_candidate_id === candidate.id ? "selected-candidate" : ""}>
                        <img src={assetUrl(candidate.asset)} alt={`seed ${candidate.seed}の候補`} />
                        <div>
                          <strong>seed {candidate.seed}</strong>
                          <small>{candidate.backend} / {candidate.status}</small>
                        </div>
                        <details>
                          <summary>生成条件</summary>
                          <small>{candidate.characters.join(", ") || "キャラ指定なし"}</small>
                          {candidate.loras.map((lora) => <small key={`${candidate.id}-${lora.node_id}`}>LoRA {lora.node_id}: {lora.lora_name}</small>)}
                          {candidate.reference_images.map((reference) => <small key={`${candidate.id}-${reference.node_id}`}>参照 {reference.node_id}: {reference.character_id}</small>)}
                          <p>{candidate.prompt}</p>
                          <p>{candidate.negative_prompt}</p>
                        </details>
                        <button
                          onClick={() => void selectCandidate(candidate.id)}
                          disabled={busy || currentPanel.selected_candidate_id === candidate.id}
                        >
                          {currentPanel.selected_candidate_id === candidate.id ? "採用中" : "採用"}
                        </button>
                      </article>
                    ))}
                  </div>
                ) : (
                  <small className="empty-candidates">生成すると候補がここに保存されます</small>
                )}
                <div className="actions">
                  <button onClick={generateCurrentPanelImage} disabled={busy}>画像生成</button>
                  <button onClick={generateCurrentPanelImage} disabled={busy}>再生成</button>
                  <button onClick={generateCurrentPageImages} disabled={busy}>ページ内全コマ生成</button>
                  <button onClick={renderCurrentPanelPage} disabled={busy}>写植更新</button>
                  <button onClick={renderCurrentPanelPage} disabled={busy}>ページ更新</button>
                  <button onClick={useStubForCurrentPanel} disabled={busy}>stubへ戻す</button>
                </div>
              </div>
            )}
          </div>

          <div className="json-pane">
            <h2>Manga JSON</h2>
            <textarea value={jsonText} onChange={(event) => setJsonText(event.target.value)} spellCheck={false} />
          </div>
        </section>
      </main>
    </div>
  );
}
