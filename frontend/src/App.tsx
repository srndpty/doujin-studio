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
  };
};

type ImageCandidate = {
  id: string;
  asset: string;
  backend: "stub" | "comfyui";
  status: "done" | "fallback" | "error";
  prompt: string;
  negative_prompt: string;
  seed: number;
  prompt_id: string | null;
  message: string;
  created_at: string;
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
  characters: { id: string; display_name: string; role: string; speech_style: string; visual_notes: string }[];
  pages: { page: number; theme: string; layout_template: string; panels: Panel[] }[];
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
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
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

  useEffect(() => {
    void refreshProjects();
    void refreshComfyStatus();
  }, []);

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
    });
  }

  async function saveJson() {
    if (!selected) return;
    await runTask(async () => {
      await saveJsonDraft("Manga JSONを保存しました");
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
    });
  }

  async function generateCurrentPageImages() {
    if (!selected || !currentPage) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("一括生成前にManga JSONを保存しました");
      const projectId = saved?.id ?? selected.id;
      let latestManga = saved?.manga_json ?? selected.manga_json;
      const panelIds = currentPage.panels.map((panel) => panel.panel_id);
      for (const [index, panelId] of panelIds.entries()) {
        setProgress({ label: `${panelId}を画像生成中`, current: index + 1, total: panelIds.length + 1 });
        const job = await createAndWaitForGenerationJob(projectId, panelId);
        if (job.status !== "done") throw new Error(job.message);
        const response = await api.get<Project>(`/api/projects/${projectId}`);
        latestManga = response.manga_json;
        setSelected({ ...(saved ?? selected), manga_json: latestManga });
        setJsonText(JSON.stringify(latestManga, null, 2));
        setAssetVersion((value) => value + 1);
      }
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
    });
  }

  async function createAndWaitForGenerationJob(projectId: string, panelId: string): Promise<GenerationJob> {
    const job = await api.post<GenerationJob>(
      `/api/projects/${projectId}/panels/${panelId}/generation-jobs`,
      { candidate_count: candidateCount }
    );
    setActiveJobId(job.id);
    try {
      return await watchGenerationJob(job);
    } finally {
      setActiveJobId(null);
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
          label: job.node ? `${job.message} (${job.node})` : job.message,
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
    if (!activeJobId) return;
    const job = await api.post<GenerationJob>(`/api/generation-jobs/${activeJobId}/cancel`);
    setMessage(job.message);
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
    });
  }

  async function exportCbz() {
    if (!selected) return;
    await runTask(async () => {
      const response = await api.post<{ cbz_asset: string }>(`/api/projects/${selected.id}/export/cbz`);
      setMessage(`CBZを書き出しました: /api/assets/${response.cbz_asset}`);
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

  function applyCommonPromptsToPanels() {
    updateManga((manga) => {
      for (const page of manga.pages) {
        for (const panel of page.panels) {
          panel.generation.prompt = mergePrompt(manga.common_positive_prompt, panel.generation.prompt || panel.prompt);
          panel.generation.negative_prompt = manga.common_negative_prompt;
        }
      }
    });
    setMessage("全コマへ共通promptを反映しました");
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
            {activeJobId && <button onClick={cancelActiveJob}>キャンセル</button>}
          </section>
        )}

        <section className="status-band">
          <strong>{comfyStatus?.backend === "comfyui" ? "ComfyUI" : "stub"}</strong>
          <span>{comfyStatus?.message ?? "接続状態を確認中"}</span>
          <small>
            workflow: {comfyStatus?.workflow_exists ? "あり" : "なし"} / node: {comfyStatus?.workflow_valid ? "OK" : "未検証"}
          </small>
        </section>

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
              <button onClick={applyCommonPromptsToPanels} disabled={busy}>全コマに反映</button>
              <button onClick={applyFourPageDraftSettings} disabled={busy}>4ページ仮設定</button>
            </div>
          </section>
        )}

        <section className="content-grid">
          <div className="preview">
            <div className="tabs">
              {[1, 2, 3, 4].map((page) => (
                <button key={page} className={selectedPage === page ? "active" : ""} onClick={() => setSelectedPage(page)}>
                  {page}p
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
