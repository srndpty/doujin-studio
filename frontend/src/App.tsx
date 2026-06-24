import {
  FormEvent,
  lazy,
  PointerEvent as ReactPointerEvent,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import {
  Download,
  FolderOpen,
  Images,
  Menu,
  PanelLeftClose,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  X
} from "lucide-react";
import { api, withRevision } from "./api/client";
import type { components } from "./api/schema";
import {
  type ProjectMutationResponse as ApiProjectMutationResponse,
  useProjectMutation
} from "./api/use-project-mutation";
import { KnowledgePanel } from "./KnowledgePanel";
import { composePromptPreview } from "./prompt-preview";
import { StoryPanel } from "./StoryPanel";

// OpenAPIスキーマを唯一の正とする。座標タプル(bbox/box)を含む型はOpenAPIが
// 固定長タプルを表現できないため、エディタ/ジオメトリ側の都合で手書きを維持する。
type Schemas = components["schemas"];

const PageEditor = lazy(() => import("./PageEditor").then((module) => ({ default: module.PageEditor })));

type WorkspaceTab = "production" | "editor" | "knowledge" | "story";

export type Dialogue = {
  speaker: string;
  text: string;
  balloon: string;
  position: string;
  box: [number, number, number, number] | null;
  font_size: number | null;
  min_font_size?: number | null;
  max_lines: number;
  vertical?: boolean;
  tail?: { enabled: boolean; tip: [number, number]; base: number; width: number } | null;
};

export type Sfx = {
  text: string;
  position: string;
  style: string;
  box?: [number, number] | null;
  font_size?: number;
  color?: string;
  outline_color?: string;
  outline_width?: number;
  rotation?: number;
  vertical?: boolean;
  layer?: "below" | "above";
};

export type Panel = {
  panel_id: string;
  bbox: [number, number, number, number];
  shape_points?: [number, number][] | null;
  shot: string;
  subject_mode?: "character_scene" | "reaction" | "prop_insert" | "hand_insert" | "background";
  role?: string;
  emphasis?: number;
  camera: string;
  location_id: string;
  characters: string[];
  prompt: string;
  image_asset: string | null;
  image_candidates: ImageCandidate[];
  selected_candidate_id: string | null;
  control_references: PanelControlReference[];
  dialogue: Dialogue[];
  sfx: Sfx[];
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
    workflow_preset_id: string | null;
    workflow_preset: WorkflowPreset | null;
    crop_scale?: number;
    crop_offset_x?: number;
    crop_offset_y?: number;
    focal_x?: number | null;
    focal_y?: number | null;
  };
};

type LoRABinding = Schemas["LoRABinding"];
type ReferenceImageBinding = Schemas["ReferenceImageBinding"];
type PanelControlReference = Schemas["PanelControlReference"];
type WorkflowPreset = Schemas["WorkflowPreset"];
type LocationProfile = Schemas["LocationProfile"];

type ImageCandidate = Schemas["ImageCandidate"];

type Character = Schemas["Character"];

type GenerationJob = Schemas["GenerationJobResponse"];

export type OverlayElement = {
  id: string;
  source_panel_id: string;
  asset: string | null;
  mask_asset: string | null;
  box: [number, number, number, number];
  scale: number;
  opacity: number;
  layer: "back" | "front";
  z_index: number;
  occluded_by_panel_ids: string[];
};

export type MangaPage = {
  page: number;
  theme: string;
  layout_template: string;
  layout_family?: string;
  intent?: string;
  layout_locked?: boolean;
  reading_order?: string[];
  overlay_elements?: OverlayElement[];
  panels: Panel[];
  render_status: "pending" | "done";
  rendered_at: string | null;
  render_asset?: string | null;
  render_hash?: string | null;
};

export type MangaProject = {
  title: string;
  work_name: string;
  premise: string;
  target_pages: number;
  common_positive_prompt: string;
  common_negative_prompt: string;
  characters: Character[];
  locations: LocationProfile[];
  workflow_presets: WorkflowPreset[];
  active_workflow_preset_id: string | null;
  reading_direction?: "rtl" | "ltr";
  typography?: {
    primary_font: string;
    default_font_size: number;
    min_font_size: number;
    vertical_default: boolean;
  };
  pages: MangaPage[];
};

type ProductionStatus = Schemas["ProjectProductionStatus"];

type ProjectSummary = Schemas["ProjectSummary"];

type ComfyUIStatus = Schemas["ComfyUIStatusResponse"];

// manga_jsonはタプル座標を含む手書きMangaProjectを使うため、ProjectDetailは別管理にする。
export type Project = {
  id: string;
  title: string;
  work_name: string;
  // 楽観ロック用。保存時に?revision=で送り、サーバ側CASで競合を検出する。
  revision: number;
  manga_json: MangaProject;
};

export type ProjectMutationResponse<T> = ApiProjectMutationResponse<Project, T>;
// adoptMutationResponseの戻り値。resynced=trueなら結果固有assetは陳腐化しているため、
// callerはproject.manga_jsonから派生状態を作り直す。
type Adopted<R> = {
  applied: boolean;
  resynced: boolean;
  project: Project | null;
  result: R;
};
type EmptyMutationResult = Schemas["EmptyMutationResult"];
type PageRenderResult = Schemas["PageRenderResult"];
type PanelImageGenerationResult = Schemas["PanelImageGenerationResult"];
type PanelPageRenderResult = Schemas["PanelPageRenderResult"];
type BatchGenerationJobResult = Schemas["BatchGenerationJobResult"];
type ExportResult = Schemas["ExportResult"];
type ReferenceAssetResult = Schemas["ReferenceAssetResult"];

type TaskProgress = {
  label: string;
  current: number;
  total: number;
  indeterminate?: boolean;
};

const ANIMA3_POSITIVE = "masterpiece, best quality, score_7, safe, anime";
const ANIMA3_NEGATIVE =
  "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia, bad hands, bad anatomy, extra fingers, missing fingers, text, watermark, speech bubble, logo";

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
  const [jobHistory, setJobHistory] = useState<GenerationJob[]>([]);
  const [showIncompleteOnly, setShowIncompleteOnly] = useState(false);
  const [controlNodeDrafts, setControlNodeDrafts] = useState<Record<string, string>>({});
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("production");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectTitle, setNewProjectTitle] = useState("テスト本");
  const selectedProjectIdRef = useRef<string | null>(null);
  // 単調性ガードを同期的に判定するためのselectedミラー。setStateの更新関数内で状態を
  // 読むと反映タイミングが保証されないため、commit済みrevisionをrefで参照する。
  const selectedRef = useRef<Project | null>(null);
  const projectLoadSequenceRef = useRef(0);
  const dragState = useRef<{
    mode: "move" | "resize";
    startX: number;
    startY: number;
    box: [number, number, number, number];
  } | null>(null);
  // 進行中の生成ウォッチャ（WebSocket/polling）の中断関数。unmount時に必ず止める。
  const jobWatchersRef = useRef<Set<() => void>>(new Set());
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  useEffect(() => {
    const baseTitle = document.title.replace(/^\(\d+\/\d+\)\s*/, "");
    if (activeJobIds.length === 0) {
      document.title = baseTitle;
      return;
    }
    const matched = progress?.label.match(/（(\d+)\/(\d+)コマ/);
    const current = matched ? Number(matched[1]) : 0;
    const total = matched ? Number(matched[2]) : activeJobIds.length;
    document.title = `(${current}/${total}) ${baseTitle}`;
    return () => {
      document.title = baseTitle;
    };
  }, [activeJobIds.length, progress?.label]);

  const currentPage = useMemo(() => {
    return selected?.manga_json.pages.find((page) => page.page === selectedPage) ?? null;
  }, [selected, selectedPage]);

  const currentPanel = useMemo(() => {
    if (!currentPage) return null;
    return (
      currentPage.panels.find((panel) => panel.panel_id === selectedPanelId) ?? currentPage.panels[0] ?? null
    );
  }, [currentPage, selectedPanelId]);

  const currentDialogue = currentPanel?.dialogue[0] ?? null;
  const effectivePrompts = useMemo(() => {
    if (!selected || !currentPanel) return { positive: "", negative: "" };
    return composePromptPreview(selected.manga_json, currentPanel);
  }, [selected, currentPanel]);
  const visiblePanels = useMemo(
    () => currentPage?.panels.filter((panel) => !showIncompleteOnly || !panel.selected_candidate_id) ?? [],
    [currentPage, showIncompleteOnly]
  );

  useEffect(() => {
    void refreshProjects();
    void refreshComfyStatus();
  }, []);

  useEffect(() => {
    // unmount時、開いているWebSocketとpollingを必ず停止する。
    const watchers = jobWatchersRef.current;
    return () => {
      for (const abort of Array.from(watchers)) abort();
      watchers.clear();
    };
  }, []);

  useEffect(() => {
    if (selected) {
      void refreshProductionStatus(selected.id);
      void refreshJobHistory(selected.id);
    }
    // プロジェクトIDが変わったときだけ再取得する（selected全体の変化では再実行しない）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  useEffect(() => {
    if (
      currentPage?.panels.length &&
      !currentPage.panels.some((panel) => panel.panel_id === selectedPanelId)
    ) {
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

  const refreshProductionStatus = useCallback(async (projectId: string) => {
    const status = await api.get<ProductionStatus>(`/api/projects/${projectId}/production-status`);
    if (selectedProjectIdRef.current === projectId) setProductionStatus(status);
  }, []);

  const refreshJobHistory = useCallback(async (projectId: string) => {
    const history = await api.get<GenerationJob[]>(`/api/projects/${projectId}/generation-jobs`);
    if (selectedProjectIdRef.current === projectId) {
      setJobHistory(history);
      setActiveJobIds(
        history.filter((job) => ["queued", "running"].includes(job.status)).map((job) => job.id)
      );
    }
  }, []);

  // selectedの単調反映を一箇所に集約する。採用判定（同一project・revision後退でない）と
  // selectedRefの前進を同期的に行い、同一tick内で複数応答が到着しても古い側を弾く。
  // 採用したときだけ jsonText を更新し、true を返す。caller は false なら派生状態を触らない。
  const commitSelectedProject = useCallback(
    (id: string, title: string, workName: string, revision: number, manga: MangaProject): boolean => {
      if (selectedProjectIdRef.current !== id) return false;
      const current = selectedRef.current;
      // 別プロジェクトへの切替途中の応答、および古いrevisionは反映しない（単調性ガード）。
      if (current && current.id !== id) return false;
      if (current && current.revision > revision) return false;
      const next: Project = {
        id,
        // ProjectRecord.title/work_nameもmanga_jsonと同時に更新される。トップレベル値も
        // 揃えないと、StoryPanel/KnowledgePanelが旧work_nameで知識同期やセッション作成をしてしまう。
        title,
        work_name: workName,
        revision,
        manga_json: manga
      };
      // 採用判定と同じ同期タイミングでrefを進め、後続応答が古い値を読まないようにする。
      selectedRef.current = next;
      setSelected((prev) => (prev === null || prev.id === id ? next : prev));
      setJsonText(JSON.stringify(manga, null, 2));
      return true;
    },
    []
  );

  const applyProjectDetail = useCallback(
    (project: Project): boolean =>
      commitSelectedProject(
        project.id,
        project.title,
        project.work_name,
        project.revision,
        project.manga_json
      ),
    [commitSelectedProject]
  );

  const refreshProjectDerivedState = useCallback(
    async (project: Project): Promise<void> => {
      if (selectedProjectIdRef.current !== project.id) return;
      const pages = project.manga_json.pages;
      setSelectedPage((current) =>
        pages.some((page) => page.page === current) ? current : (pages[0]?.page ?? 1)
      );
      setSelectedPanelId((current) => {
        const panelIds = new Set(pages.flatMap((page) => page.panels.map((panel) => panel.panel_id)));
        return current && panelIds.has(current) ? current : (pages[0]?.panels[0]?.panel_id ?? null);
      });
      setPageAssets(pages.map((page) => page.render_asset ?? ""));
      await Promise.all([refreshProductionStatus(project.id), refreshJobHistory(project.id)]);
    },
    [refreshJobHistory, refreshProductionStatus]
  );

  // PageEditorのローカル編集など、revisionを進めずmanga_jsonだけ差し替える反映。
  // revision未指定時は現在のrevisionを維持しつつ、単調性ガードは commitSelectedProject に委ねる。
  function applyProjectMutation(projectId: string, mangaJson: MangaProject, revision?: number): boolean {
    const current = selectedRef.current;
    const effectiveRevision = revision ?? (current && current.id === projectId ? current.revision : 0);
    return commitSelectedProject(
      projectId,
      mangaJson.title,
      mangaJson.work_name,
      effectiveRevision,
      mangaJson
    );
  }

  const onProjectConflict = useCallback(
    async (project: Project) => {
      await refreshProjectDerivedState(project);
      setMessage("他の操作で更新されたため最新を採用しました。未保存編集は適用されませんでした。");
    },
    [refreshProjectDerivedState]
  );
  const { handleProjectMutationError } = useProjectMutation<Project>({
    applyProject: applyProjectDetail,
    onConflict: onProjectConflict,
    onStale: (projectId) => {
      void reloadSelectedProject(projectId);
    },
    onPartialSuccess: async (project, _completed, _failed) => {
      // 候補採用などの前段は適用済み。最新stateを採用しつつ、後段失敗のみ通知する。
      await refreshProjectDerivedState(project);
      setMessage(
        "候補を採用しましたが、ページの再描画が競合しました。最新状態で再度レンダリングしてください。"
      );
    }
  });

  function updateProjectPageAssets(projectId: string, update: (assets: string[]) => string[]) {
    if (selectedProjectIdRef.current === projectId) setPageAssets(update);
  }

  async function runTask(task: () => Promise<void>) {
    setBusy(true);
    setProgress({ label: "処理中", current: 0, total: 100, indeterminate: true });
    try {
      await task();
    } catch (error) {
      if (!(await handleProjectMutationError(error))) {
        setMessage(error instanceof Error ? error.message : "処理に失敗しました");
      }
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!newProjectTitle.trim()) return;
    await runTask(async () => {
      const response = await api.post<ProjectMutationResponse<EmptyMutationResult>>("/api/projects", {
        title: newProjectTitle.trim(),
        work_name: "",
        target_pages: 4
      });
      const project = response.project;
      selectedProjectIdRef.current = project.id;
      setSelected(project);
      setSelectedPage(1);
      setSelectedPanelId(project.manga_json.pages[0]?.panels[0]?.panel_id ?? null);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets(project.manga_json.pages.map((page) => page.render_asset ?? ""));
      await refreshProjects();
      setNewProjectOpen(false);
      setActiveTab("story");
      setMessage("プロジェクトを作成しました");
    });
  }

  async function deleteProject(project: ProjectSummary) {
    if (!window.confirm(`プロジェクト「${project.title}」を削除します。生成画像も元に戻せません。`)) return;
    await runTask(async () => {
      const result = await api.delete<Schemas["ProjectDeletionResponse"]>(`/api/projects/${project.id}`);
      if (selectedProjectIdRef.current === project.id) {
        selectedProjectIdRef.current = null;
        selectedRef.current = null;
        setSelected(null);
        setJsonText("");
        setPageAssets([]);
        setProductionStatus(null);
        setJobHistory([]);
        setActiveJobIds([]);
      }
      await refreshProjects();
      const notices: string[] = [];
      if (result.cleanup_state === "manual_required") {
        notices.push(
          `プロジェクトは削除済みですが成果物を削除できません。対象フォルダを閉じてから手動削除してください: ${result.manual_cleanup_path ?? "保存先を確認してください"}`
        );
      } else if (result.cleanup_state === "pending") {
        notices.push("プロジェクトは削除済みです。残存ファイルは次回起動時に再回収します");
      } else {
        notices.push("プロジェクトを削除しました");
      }
      if (result.generation_stop_failed) {
        notices.push("生成停止に失敗したため、成果物が再出現する可能性があります");
      }
      setMessage(notices.join(" / "));
    });
  }

  async function saveProjectTitle() {
    if (!selected) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("タイトルとManga JSONを保存しました");
      if (!saved) return;
      await refreshProjects();
    });
  }

  async function loadProject(id: string) {
    const sequence = ++projectLoadSequenceRef.current;
    const previousId = selectedProjectIdRef.current;
    selectedProjectIdRef.current = id;
    await runTask(async () => {
      let project: Project;
      try {
        project = await api.get<Project>(`/api/projects/${id}`);
      } catch (error) {
        if (projectLoadSequenceRef.current === sequence) selectedProjectIdRef.current = previousId;
        throw error;
      }
      if (projectLoadSequenceRef.current !== sequence || selectedProjectIdRef.current !== id) return;
      // 単調性ガードの基準となるrefを同期的に切替先へ進める（useEffect同期を待たない）。
      selectedRef.current = project;
      setSelected(project);
      setSelectedPage(1);
      setSelectedPanelId(project.manga_json.pages[0]?.panels[0]?.panel_id ?? null);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets(project.manga_json.pages.map((page) => page.render_asset ?? ""));
      setProductionStatus(null);
      setJobHistory([]);
      setActiveJobIds([]);
      setMessage("プロジェクトを読み込みました");
    });
  }

  // Manga JSON保存を一本化し、必ず?revision=を添えて楽観ロックを効かせる。
  // 自分の操作で進んだrevisionは各応答から同期しているため、ここでの409は
  // 「他の操作・他タブによる実際の競合」を意味する。古い全文での上書きは危険なので、
  // 安全側に倒して最新を採用(reload)し、未保存の編集は適用しない（暗黙の上書きをしない）。
  // ※base/local/latestの三者マージUIは将来の改善として残す。
  // latest_revisionを捨てないよう応答全体を返す。callerは adoptMutationResponse 経由で
  // 反映し、必要なら最新へ再同期する。
  async function putMangaJson(
    projectId: string,
    revision: number,
    manga: MangaProject
  ): Promise<ProjectMutationResponse<EmptyMutationResult>> {
    try {
      return await api.put<ProjectMutationResponse<EmptyMutationResult>>(
        withRevision(`/api/projects/${projectId}/manga-json`, revision),
        manga
      );
    } catch (error) {
      if (await handleProjectMutationError(error)) {
        throw new Error(
          "他の操作で更新されていたため最新を採用しました。未保存の編集は適用されませんでした。"
        );
      }
      throw error;
    }
  }

  // ジョブ終端後など、サーバ側でmanga_json/revisionが進んだ経路で最新を丸ごと反映する。
  // revisionだけ先行させると古いmanga_jsonを最新revisionで保存でき、サーバの候補・生成状態を
  // 巻き戻せてしまうため、必ずmanga_jsonごと取り込む。反映後の最新projectを返す。
  async function reloadSelectedProject(projectId: string): Promise<Project | null> {
    const latest = await api.get<Project>(`/api/projects/${projectId}`);
    if (selectedProjectIdRef.current !== projectId) return null;
    if (!applyProjectDetail(latest)) {
      const current = selectedRef.current;
      return current?.id === projectId ? current : null;
    }
    return selectedRef.current?.id === projectId ? selectedRef.current : null;
  }

  // ProjectMutationResponse を反映し、latest_revisionが応答snapshotより新しければ最新へ
  // 再同期する。後続mutationのあるフローは戻り値の project.revision を次のAPIへ渡せる。
  // applied=false（巻き戻し回避で未反映）なら派生状態を触らない。
  // resynced=true のとき、応答固有のresult（古いpage_asset等）はもう最新ではないので、
  // callerは結果固有assetではなく project.manga_json から派生状態を作り直す。
  async function adoptMutationResponse<R>(response: ProjectMutationResponse<R>): Promise<Adopted<R>> {
    let applied = applyProjectDetail(response.project);
    let project: Project | null = applied ? selectedRef.current : null;
    let resynced = false;
    if (applied && response.latest_revision > response.project.revision) {
      project = await reloadSelectedProject(response.project.id);
      resynced = true;
      if (!project || project.id !== response.project.id) {
        applied = false;
        project = null;
      }
    }
    return { applied, resynced, project, result: response.result };
  }

  // render系応答のpage_assetをプレビューへ反映する共通処理。再同期済みなら結果固有の
  // 古いpage_assetは使わず、常に最新manga_jsonのrender_assetからプレビューを作り直す。
  function applyRenderedPageAsset(adopted: Adopted<{ page_asset: string }>, _pageNumber?: number): void {
    if (!adopted.applied || !adopted.project) return;
    const project = adopted.project;
    updateProjectPageAssets(project.id, () =>
      project.manga_json.pages.map((page) => page.render_asset ?? "")
    );
    setAssetVersion((value) => value + 1);
  }

  // 更新APIの応答(manga_json + revision)をselectedへ反映する。
  // revisionを必ず同期し、サーバ側でrevisionが進んだ後の保存が誤って409にならないようにする。
  async function saveJsonDraft(successMessage: string): Promise<Project | null> {
    if (!selected) return null;
    const parsed = JSON.parse(jsonText) as MangaProject;
    const { applied, project } = await adoptMutationResponse(
      await putMangaJson(selected.id, selected.revision, parsed)
    );
    if (!applied) return null;
    setMessage(successMessage);
    return project;
  }

  async function renderPages() {
    if (!selected) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("レンダリング前にManga JSONを保存しました");
      if (!saved) return;
      const projectId = saved.id;
      const manga = saved.manga_json;
      let latestRevision = saved.revision;
      for (const page of manga.pages) {
        if (selectedProjectIdRef.current !== projectId) return;
        const firstPanel = page.panels[0];
        if (!firstPanel) continue;
        setProgress({
          label: `${page.page}ページをレンダリング中`,
          current: page.page,
          total: manga.pages.length
        });
        const response = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
          withRevision(`/api/projects/${projectId}/panels/${firstPanel.panel_id}/render-page`, latestRevision)
        );
        const adopted = await adoptMutationResponse(response);
        if (!adopted.applied || !adopted.project) return;
        // 次のrender-pageへは反映済みの最新revisionを渡す（再同期で進んだ場合も追従）。
        latestRevision = adopted.project.revision;
        applyRenderedPageAsset(adopted, page.page);
        if (adopted.resynced) {
          setMessage("構成が更新されたためレンダリングを中断しました。再実行してください。");
          await refreshProductionStatus(projectId);
          return;
        }
      }
      setMessage("ページをレンダリングしました");
      await refreshProductionStatus(projectId);
    });
  }

  async function generateCurrentPanelImage() {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      setProgress({ label: "Manga JSONを保存中", current: 1, total: 4 });
      const saved = await saveJsonDraft("生成前にManga JSONを保存しました");
      if (!saved) return;
      const projectId = saved.id;
      const job = await createAndWaitForGenerationJob(projectId, currentPanel.panel_id, saved.revision);
      if (selectedProjectIdRef.current !== projectId) return;
      if (job.status !== "done") throw new Error(job.message);
      const fresh = await api.get<Project>(`/api/projects/${projectId}`);
      if (selectedProjectIdRef.current !== projectId || !applyProjectDetail(fresh)) return;
      setProgress({ label: `${selectedPage}ページを更新中`, current: 3, total: 4 });
      const pageResponse = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
        withRevision(
          `/api/projects/${projectId}/panels/${currentPanel.panel_id}/render-page`,
          selectedRef.current?.revision ?? fresh.revision
        )
      );
      const adopted = await adoptMutationResponse(pageResponse);
      if (!adopted.applied || !adopted.project) return;
      applyRenderedPageAsset(adopted, selectedPage);
      if (adopted.resynced) {
        setMessage("構成が更新されたためページ更新を中断しました。再実行してください。");
        return;
      }
      setProgress({ label: "プレビューを更新しました", current: 4, total: 4 });
      setMessage(`${currentPanel.panel_id}に候補を${candidateCount}件追加しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function generateCurrentPageImages() {
    if (!selected || !currentPage) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("一括生成前にManga JSONを保存しました");
      if (!saved) return;
      const projectId = saved.id;
      const savedPage = saved.manga_json.pages.find((page) => page.page === selectedPage);
      if (!savedPage) return;
      const panelIds = savedPage.panels.map((panel) => panel.panel_id);
      const batchResponse = await api.post<ProjectMutationResponse<BatchGenerationJobResult>>(
        withRevision(`/api/projects/${projectId}/generation-jobs`, saved.revision),
        {
          page: selectedPage,
          candidate_count: candidateCount
        }
      );
      const batchAdopted = await adoptMutationResponse(batchResponse);
      if (!batchAdopted.applied || !batchAdopted.project) return;
      const batch = batchAdopted.result;
      setActiveJobIds(batch.jobs.map((job) => job.id));
      try {
        for (const job of batch.jobs) {
          const completed = await watchGenerationJob(job);
          if (completed.status !== "done") throw new Error(completed.message);
        }
      } finally {
        setActiveJobIds([]);
        await refreshJobHistory(projectId);
        await reloadSelectedProject(projectId);
      }
      if (selectedProjectIdRef.current !== projectId) return;
      const firstPanelId = panelIds[0];
      if (firstPanelId) {
        setProgress({
          label: `${selectedPage}ページを更新中`,
          current: panelIds.length + 1,
          total: panelIds.length + 1
        });
        const pageResponse = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
          withRevision(
            `/api/projects/${projectId}/panels/${firstPanelId}/render-page`,
            selectedRef.current?.revision ?? selected.revision
          )
        );
        const adopted = await adoptMutationResponse(pageResponse);
        if (!adopted.applied || !adopted.project) return;
        applyRenderedPageAsset(adopted, selectedPage);
        if (adopted.resynced) {
          setMessage("構成が更新されたためページ更新を中断しました。再実行してください。");
          return;
        }
      }
      setAssetVersion((value) => value + 1);
      setMessage(`${selectedPage}ページの全コマを生成しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function generateAllPageImages() {
    if (!selected) return;
    void requestNotificationPermission();
    await runTask(async () => {
      const saved = await saveJsonDraft("全ページ生成前にManga JSONを保存しました");
      if (!saved) return;
      const projectId = saved.id;
      const batchResponse = await api.post<ProjectMutationResponse<BatchGenerationJobResult>>(
        withRevision(`/api/projects/${projectId}/generation-jobs`, saved.revision),
        { candidate_count: candidateCount }
      );
      const batchAdopted = await adoptMutationResponse(batchResponse);
      if (!batchAdopted.applied || !batchAdopted.project) return;
      const batch = batchAdopted.result;
      setActiveJobIds(batch.jobs.map((job) => job.id));
      try {
        await waitForBatchJobs(batch.jobs, "全ページの画像を生成中");
      } finally {
        setActiveJobIds([]);
        await refreshJobHistory(projectId);
        await reloadSelectedProject(projectId);
      }
      if (selectedProjectIdRef.current !== projectId) return;
      const latest = await reloadSelectedProject(projectId);
      if (!latest || latest.id !== projectId || selectedProjectIdRef.current !== projectId) return;
      const completed = await renderAllPages(projectId, latest);
      if (!completed) return;
      // page_*.pngを書き換えた後にキャッシュバスターを進める（同一URLの古い画像が残らないように）。
      setAssetVersion((value) => value + 1);
      await refreshProductionStatus(projectId);
      setMessage("全ページの画像生成とレンダリングが完了しました");
      notifyCompletion("全ページ生成が完了しました", latest.title);
    });
  }

  async function waitForBatchJobs(initialJobs: GenerationJob[], label: string): Promise<void> {
    let jobs = initialJobs;
    while (true) {
      jobs = await Promise.all(jobs.map((job) => api.get<GenerationJob>(`/api/generation-jobs/${job.id}`)));
      const completed = jobs.filter((job) => ["done", "error", "cancelled"].includes(job.status)).length;
      const progressTotal = jobs.reduce((sum, job) => sum + job.progress, 0);
      setProgress({
        label: `${label}（${completed}/${jobs.length}コマ完了）`,
        current: progressTotal,
        total: jobs.length * 100
      });
      if (completed === jobs.length) break;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    const failed = jobs.find((job) => job.status !== "done");
    if (failed) throw new Error(failed.message);
  }

  async function renderAllPages(projectId: string, project: Project): Promise<boolean> {
    if (project.id !== projectId || selectedProjectIdRef.current !== projectId) return false;
    let latestRevision = project.revision;
    for (let index = 0; index < project.manga_json.pages.length; index += 1) {
      if (selectedProjectIdRef.current !== projectId) return false;
      const page = project.manga_json.pages[index];
      const firstPanel = page.panels[0];
      if (!firstPanel) continue;
      setProgress({
        label: `${page.page}ページをレンダリング中`,
        current: index,
        total: project.manga_json.pages.length
      });
      const response = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
        withRevision(`/api/projects/${projectId}/panels/${firstPanel.panel_id}/render-page`, latestRevision)
      );
      if (selectedProjectIdRef.current !== projectId) return false;
      const adopted = await adoptMutationResponse(response);
      if (!adopted.applied || !adopted.project) return false;
      latestRevision = adopted.project.revision;
      applyRenderedPageAsset(adopted, page.page);
      if (adopted.resynced) {
        setMessage("構成が更新されたためレンダリングを中断しました。再実行してください。");
        await refreshProductionStatus(projectId);
        return false;
      }
    }
    return true;
  }

  async function requestNotificationPermission(): Promise<void> {
    if ("Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
  }

  function notifyCompletion(title: string, body: string): void {
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification(title, { body });
    }
  }

  async function createAndWaitForGenerationJob(
    projectId: string,
    panelId: string,
    revision: number
  ): Promise<GenerationJob> {
    const response = await api.post<ProjectMutationResponse<GenerationJob>>(
      withRevision(`/api/projects/${projectId}/panels/${panelId}/generation-jobs`, revision),
      { candidate_count: candidateCount }
    );
    const adopted = await adoptMutationResponse(response);
    if (!adopted.applied || !adopted.project) {
      throw new Error("プロジェクトが切り替わったため処理を中断しました。");
    }
    const job = adopted.result;
    setActiveJobIds([job.id]);
    try {
      return await watchGenerationJob(job);
    } finally {
      setActiveJobIds([]);
      await refreshJobHistory(projectId);
      // ジョブ登録・生成・失敗・キャンセルでサーバ側のmanga_json/revisionが進むため、
      // 最新プロジェクト全体を取り込む（revisionだけ進めて古いJSONを残さない）。
      await reloadSelectedProject(projectId);
    }
  }

  function watchGenerationJob(initialJob: GenerationJob): Promise<GenerationJob> {
    const TERMINAL = ["done", "error", "cancelled"];
    const MAX_POLL_ATTEMPTS = 8;
    return new Promise<GenerationJob>((resolve, reject) => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      let settled = false;
      let socket: WebSocket | null = null;
      let pollTimer: number | null = null;
      let pollFailures = 0;

      const cleanup = () => {
        if (socket) {
          socket.onmessage = socket.onerror = socket.onclose = null;
          try {
            socket.close();
          } catch {
            /* 既にクローズ済みでも無視する */
          }
          socket = null;
        }
        if (pollTimer !== null) {
          window.clearTimeout(pollTimer);
          pollTimer = null;
        }
        jobWatchersRef.current.delete(abort);
      };
      const finish = (job: GenerationJob) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(job);
      };
      const fail = (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error instanceof Error ? error : new Error("生成ジョブの監視に失敗しました"));
      };
      // unmountやキャンセルから安全に止めるための中断関数。
      const abort = () => fail(new Error("生成ジョブの監視を中断しました"));
      jobWatchersRef.current.add(abort);

      const update = (job: GenerationJob) => {
        setProgress({
          label: job.node
            ? `${job.panel_id}: ${job.message} (${job.node})`
            : `${job.panel_id}: ${job.message}`,
          current: job.progress,
          total: 100,
          indeterminate: job.status === "queued" && job.progress === 0
        });
        if (TERMINAL.includes(job.status)) finish(job);
      };

      const poll = async () => {
        if (settled) return;
        try {
          const job = await api.get<GenerationJob>(`/api/generation-jobs/${initialJob.id}`);
          pollFailures = 0;
          update(job);
          if (settled) return;
        } catch (error) {
          pollFailures += 1;
          if (pollFailures >= MAX_POLL_ATTEMPTS) {
            fail(error);
            return;
          }
        }
        if (settled) return;
        // 指数バックオフ（1s,2s,4s…最大8s）で再接続を試みる。
        const delay = Math.min(1000 * 2 ** Math.max(0, pollFailures), 8000);
        setProgress({
          label: pollFailures > 0 ? `再接続中 (${pollFailures}回目)…` : "生成状況を確認中…",
          current: 0,
          total: 100,
          indeterminate: true
        });
        pollTimer = window.setTimeout(() => void poll(), delay);
      };

      const startPolling = () => {
        if (settled) return;
        if (socket) {
          socket.onmessage = socket.onerror = socket.onclose = null;
          try {
            socket.close();
          } catch {
            /* 無視 */
          }
          socket = null;
        }
        if (pollTimer === null) void poll();
      };

      try {
        socket = new WebSocket(
          `${protocol}//${window.location.host}/api/generation-jobs/${initialJob.id}/ws`
        );
        socket.onmessage = (event) => {
          try {
            update(JSON.parse(event.data) as GenerationJob);
          } catch (error) {
            fail(error);
          }
        };
        socket.onerror = startPolling;
        socket.onclose = startPolling;
      } catch {
        // WebSocketを開けない環境ではpollingへ退避する。
        startPolling();
      }
    });
  }

  async function cancelActiveJob() {
    if (activeJobIds.length === 0) return;
    await Promise.all(
      activeJobIds.map((jobId) =>
        api.post<ProjectMutationResponse<GenerationJob>>(`/api/generation-jobs/${jobId}/cancel`)
      )
    );
    if (selected) await reloadSelectedProject(selected.id);
    setMessage("生成キューをキャンセルしました");
  }

  async function selectCandidate(candidateId: string) {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      const response = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
        withRevision(
          `/api/projects/${selected.id}/panels/${currentPanel.panel_id}/candidates/${candidateId}/select`,
          selected.revision
        )
      );
      const adopted = await adoptMutationResponse(response);
      if (!adopted.applied || !adopted.project) return;
      applyRenderedPageAsset(adopted, selectedPage);
      if (adopted.resynced) {
        setMessage("構成が更新されたため候補採用結果を再同期しました");
        return;
      }
      setMessage("画像候補を採用し、ページを更新しました");
      await refreshProductionStatus(selected.id);
    });
  }

  async function useStubForCurrentPanel() {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("stub生成前にManga JSONを保存しました");
      if (!saved) return;
      const projectId = saved.id;
      // use-stubの応答を先に反映する。renderが失敗してもrevisionが古いまま残らない。
      const stubResponse = await api.post<ProjectMutationResponse<PanelImageGenerationResult>>(
        withRevision(`/api/projects/${projectId}/panels/${currentPanel.panel_id}/use-stub`, saved.revision)
      );
      const stubAdopted = await adoptMutationResponse(stubResponse);
      if (!stubAdopted.applied || !stubAdopted.project) return;
      const pageResponse = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
        withRevision(
          `/api/projects/${projectId}/panels/${currentPanel.panel_id}/render-page`,
          stubAdopted.project.revision
        )
      );
      const pageAdopted = await adoptMutationResponse(pageResponse);
      if (!pageAdopted.applied || !pageAdopted.project) return;
      applyRenderedPageAsset(pageAdopted, selectedPage);
      if (pageAdopted.resynced) {
        setMessage("構成が更新されたためstub結果を再同期しました");
        return;
      }
      setMessage(`${currentPanel.panel_id}をstub画像へ戻しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function renderCurrentPanelPage() {
    if (!selected || !currentPanel) return;
    await runTask(async () => {
      const saved = await saveJsonDraft("写植更新前にManga JSONを保存しました");
      if (!saved) return;
      const projectId = saved.id;
      const response = await api.post<ProjectMutationResponse<PanelPageRenderResult>>(
        withRevision(`/api/projects/${projectId}/panels/${currentPanel.panel_id}/render-page`, saved.revision)
      );
      const adopted = await adoptMutationResponse(response);
      if (!adopted.applied || !adopted.project) return;
      applyRenderedPageAsset(adopted, selectedPage);
      if (adopted.resynced) {
        setMessage("構成が更新されたためページを再同期しました");
        return;
      }
      setMessage(`${selectedPage}ページを更新しました`);
      await refreshProductionStatus(projectId);
    });
  }

  async function exportCbz() {
    if (!selected) return;
    await runTask(async () => {
      const projectId = selected.id;
      const response = await api.post<ProjectMutationResponse<ExportResult>>(
        withRevision(`/api/projects/${projectId}/export/cbz`, selected.revision)
      );
      const adopted = await adoptMutationResponse(response);
      if (!adopted.applied || !adopted.project) return;
      const { result } = adopted;
      const warnings = result.warnings ?? [];
      const warning = warnings.length ? ` / 警告 ${warnings.length}件` : "";
      setMessage(`CBZを書き出しました: ${result.absolute_path}${warning}`);
      await refreshProductionStatus(projectId);
    });
  }

  async function openExportFolder() {
    if (!selected) return;
    await runTask(async () => {
      const response = await api.post<{ folder_path: string; cbz_path: string; cbz_exists: boolean }>(
        `/api/projects/${selected.id}/export/open-folder`
      );
      setMessage(
        response.cbz_exists
          ? `保存先を開きました: ${response.cbz_path}`
          : `出力フォルダを開きました: ${response.folder_path}`
      );
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
      manga.common_positive_prompt = ANIMA3_POSITIVE;
      manga.common_negative_prompt = ANIMA3_NEGATIVE;
    });
  }

  function applyFourPageDraftSettings() {
    const pageConfigs: Record<
      number,
      {
        prompt: string;
        width: number;
        height: number;
        fit: "cover" | "contain";
        anchor: "center" | "top" | "bottom" | "left" | "right";
      }
    > = {
      1: {
        prompt: "establishing shot, after school room, soft daylight, calm mood",
        width: 1024,
        height: 640,
        fit: "cover",
        anchor: "center"
      },
      2: {
        prompt: "two character conversation, expressive faces, medium shot, clean background",
        width: 896,
        height: 640,
        fit: "cover",
        anchor: "center"
      },
      3: {
        prompt: "dynamic reaction, comedic timing, energetic pose, manga composition",
        width: 896,
        height: 672,
        fit: "cover",
        anchor: "top"
      },
      4: {
        prompt: "punchline scene, comedic contrast, clear silhouettes, final panel emphasis",
        width: 1024,
        height: 768,
        fit: "cover",
        anchor: "center"
      }
    };
    updateManga((manga) => {
      for (const page of manga.pages) {
        const config = pageConfigs[page.page];
        if (!config) continue;
        for (const panel of page.panels) {
          panel.generation.prompt = mergePrompt(config.prompt, panel.generation.prompt || panel.prompt);
          panel.generation.negative_prompt = manga.common_negative_prompt || ANIMA3_NEGATIVE;
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
          font_size: null,
          min_font_size: null,
          max_lines: 6,
          vertical: selected?.manga_json.typography?.vertical_default ?? true
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

  function addWorkflowPreset() {
    updateManga((manga) => {
      let index = manga.workflow_presets.length + 1;
      while (manga.workflow_presets.some((preset) => preset.id === `preset_${index}`)) index += 1;
      manga.workflow_presets.push({
        id: `preset_${index}`,
        name: `プリセット${index}`,
        checkpoint_node_id: "",
        checkpoint_name: "",
        vae_node_id: "",
        vae_name: "",
        sampler_node_id: "",
        sampler_name: "",
        scheduler: "",
        steps: null,
        cfg: null,
        denoise: null
      });
      manga.active_workflow_preset_id ??= `preset_${index}`;
    });
  }

  function updateWorkflowPreset(presetId: string, mutator: (preset: WorkflowPreset) => void) {
    updateManga((manga) => {
      const preset = manga.workflow_presets.find((item) => item.id === presetId);
      if (preset) mutator(preset);
    });
  }

  function addLocation() {
    updateManga((manga) => {
      let index = manga.locations.length + 1;
      while (manga.locations.some((location) => location.id === `location_${index}`)) index += 1;
      manga.locations.push({
        id: `location_${index}`,
        display_name: `ロケーション${index}`,
        prompt: "",
        negative_prompt: "",
        reference_image_asset: null,
        reference_load_node_id: ""
      });
    });
  }

  function updateLocation(locationId: string, mutator: (location: LocationProfile) => void) {
    updateManga((manga) => {
      const location = manga.locations.find((item) => item.id === locationId);
      if (location) mutator(location);
    });
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
    const projectId = selected.id;
    const revision = selected.revision;
    await runTask(async () => {
      const response = await api.postBinary<ProjectMutationResponse<ReferenceAssetResult>>(
        withRevision(`/api/projects/${projectId}/characters/${characterId}/reference-image`, revision),
        file
      );
      if ((await adoptMutationResponse(response)).applied) setAssetVersion((value) => value + 1);
      setMessage("参照画像を登録しました");
    });
  }

  async function uploadLocationImage(locationId: string, file: File) {
    if (!selected) return;
    await uploadProjectImage(
      selected.id,
      withRevision(`/api/projects/${selected.id}/locations/${locationId}/reference-image`, selected.revision),
      file,
      "ロケーション参照画像を登録しました"
    );
  }

  async function uploadControlImage(kind: PanelControlReference["kind"], file: File) {
    if (!selected || !currentPanel) return;
    const existing = currentPanel.control_references.find((item) => item.kind === kind);
    const nodeId = existing?.load_node_id || controlNodeDrafts[kind] || "";
    if (!nodeId) {
      setMessage("Control参照のLoadImageノードIDを入力してください");
      return;
    }
    await uploadProjectImage(
      selected.id,
      withRevision(
        `/api/projects/${selected.id}/panels/${currentPanel.panel_id}/controls/${kind}/reference-image?load_node_id=${encodeURIComponent(nodeId)}`,
        selected.revision
      ),
      file,
      `${kind}参照画像を登録しました`
    );
  }

  async function uploadProjectImage(_projectId: string, path: string, file: File, successMessage: string) {
    await runTask(async () => {
      const response = await api.postBinary<ProjectMutationResponse<ReferenceAssetResult>>(path, file);
      if ((await adoptMutationResponse(response)).applied) setAssetVersion((value) => value + 1);
      setMessage(successMessage);
    });
  }

  function startBalloonDrag(event: ReactPointerEvent<HTMLElement>, mode: "move" | "resize") {
    const box = currentDialogue?.box ?? [0.48, 0.06, 0.46, 0.22];
    dragState.current = {
      mode,
      startX: event.clientX,
      startY: event.clientY,
      box: [...box] as [number, number, number, number]
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveBalloon(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragState.current;
    const editor = event.currentTarget.parentElement;
    if (!drag || !editor) return;
    const rect = editor.getBoundingClientRect();
    const dx = (event.clientX - drag.startX) / rect.width;
    const dy = (event.clientY - drag.startY) / rect.height;
    updateCurrentDialogue((dialogue) => {
      const next = [...drag.box] as [number, number, number, number];
      if (drag.mode === "move") {
        next[0] = clamp(drag.box[0] + dx, 0, 1 - drag.box[2]);
        next[1] = clamp(drag.box[1] + dy, 0, 1 - drag.box[3]);
      } else {
        next[2] = clamp(drag.box[2] + dx, 0.08, 1 - drag.box[0]);
        next[3] = clamp(drag.box[3] + dy, 0.06, 1 - drag.box[1]);
      }
      dialogue.box = next;
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
    <div className={`app-shell ${sidebarOpen ? "" : "sidebar-closed"}`}>
      <aside className="sidebar">
        <div className="sidebar-heading">
          <h1>Doujin Studio</h1>
          <button className="icon-button" title="サイドバーを閉じる" onClick={() => setSidebarOpen(false)}>
            <PanelLeftClose size={18} />
          </button>
        </div>
        <div className="project-list">
          <div className="section-heading">
            <h2>プロジェクト</h2>
            <button
              className="icon-button"
              title="新規プロジェクト"
              onClick={() => setNewProjectOpen(true)}
              disabled={busy}
            >
              <Plus size={18} />
            </button>
          </div>
          {projects.map((project) => (
            <div
              key={project.id}
              className={`project-list-item ${selected?.id === project.id ? "selected" : ""}`}
            >
              <button onClick={() => void loadProject(project.id)}>
                <span>{project.title}</span>
                <small>{project.work_name || "作品名未設定"}</small>
              </button>
              <button
                className="project-delete"
                title={`${project.title}を削除`}
                aria-label={`${project.title}を削除`}
                onClick={() => void deleteProject(project)}
                disabled={busy}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <main className="workspace">
        <header className="toolbar">
          <button
            className="icon-button"
            title="プロジェクト一覧"
            onClick={() => setSidebarOpen((value) => !value)}
          >
            <Menu size={20} />
          </button>
          <div className="project-heading">
            {selected ? (
              <input
                className="project-title-input"
                aria-label="本のタイトル"
                value={selected.manga_json.title}
                onChange={(event) =>
                  updateManga((manga) => {
                    manga.title = event.target.value;
                  })
                }
              />
            ) : (
              <strong>プロジェクト未選択</strong>
            )}
            <span>{message}</span>
          </div>
          <div className="actions">
            <button
              className="icon-button"
              title="ComfyUI接続状態を再検出"
              onClick={() => void refreshComfyStatus()}
              disabled={busy}
            >
              <RefreshCw size={18} />
            </button>
            <button title="タイトルと編集内容を保存" onClick={saveProjectTitle} disabled={!selected || busy}>
              <Save size={17} />
              保存
            </button>
            <button title="全ページを生成" onClick={generateAllPageImages} disabled={!selected || busy}>
              <Images size={17} />
              全ページ生成
            </button>
            <button title="全ページをレンダリング" onClick={renderPages} disabled={!selected || busy}>
              <RefreshCw size={17} />
              レンダリング
            </button>
            <button title="CBZを書き出す" onClick={exportCbz} disabled={!selected || busy}>
              <Download size={17} />
              CBZ
            </button>
            <button title="保存先を開く" onClick={openExportFolder} disabled={!selected || busy}>
              <FolderOpen size={17} />
              保存先
            </button>
          </div>
        </header>

        <nav className="workspace-tabs">
          <button
            className={activeTab === "story" ? "active" : ""}
            onClick={() => setActiveTab("story")}
            disabled={!selected}
          >
            ストーリー生成
          </button>
          <button
            className={activeTab === "production" ? "active" : ""}
            onClick={() => setActiveTab("production")}
          >
            制作
          </button>
          <button
            className={activeTab === "editor" ? "active" : ""}
            onClick={() => setActiveTab("editor")}
            disabled={!selected}
          >
            ページ編集
          </button>
          <button
            className={activeTab === "knowledge" ? "active" : ""}
            onClick={() => setActiveTab("knowledge")}
          >
            作品知識
          </button>
        </nav>

        {progress && (
          <section className="progress-band" role="status" aria-live="polite">
            <div>
              <strong>{progress.label}</strong>
              <span>
                {progress.current} / {progress.total}
              </span>
            </div>
            <progress
              className={progress.indeterminate ? "indeterminate" : ""}
              value={progress.indeterminate ? undefined : progressPercent}
              max="100"
            />
            {activeJobIds.length > 0 && <button onClick={cancelActiveJob}>キャンセル</button>}
          </section>
        )}

        {activeTab === "editor" && selected && (
          <Suspense fallback={<p className="hint">ページ編集機能を読み込んでいます...</p>}>
            <PageEditor
              projectId={selected.id}
              revision={selected.revision}
              manga={selected.manga_json}
              pageNumber={selectedPage}
              assetVersion={assetVersion}
              busy={busy}
              setMessage={setMessage}
              onChange={(manga, revision) => applyProjectMutation(selected.id, manga, revision)}
              onOverlayUpload={async (manga, pageNumber, overlayId, kind, file) => {
                const projectId = selected.id;
                try {
                  const saved = await adoptMutationResponse(
                    await putMangaJson(projectId, selected.revision, manga)
                  );
                  if (!saved.applied || !saved.project) return false;
                  const response = await api.postBinary<ProjectMutationResponse<ReferenceAssetResult>>(
                    withRevision(
                      `/api/projects/${projectId}/pages/${pageNumber}/overlays/${encodeURIComponent(overlayId)}/${kind}`,
                      saved.project.revision
                    ),
                    file
                  );
                  const adopted = await adoptMutationResponse(response);
                  if (adopted.applied && adopted.project) {
                    setAssetVersion((value) => value + 1);
                  }
                  return adopted.applied && !!adopted.project && !adopted.resynced;
                } catch (error) {
                  if (await handleProjectMutationError(error)) return false;
                  throw error;
                }
              }}
              onSave={async (manga) => {
                if (!selected) return;
                const projectId = selected.id;
                const pageNumber = selectedPage;
                setBusy(true);
                try {
                  // 保存成功時点のrevisionを先に反映する。直後のrenderが失敗しても、
                  // サーバが進めたrevisionとselectedが乖離して次の保存が誤409にならないように。
                  const saved = await adoptMutationResponse(
                    await putMangaJson(projectId, selected.revision, manga)
                  );
                  if (!saved.applied || !saved.project) return;
                  const rendered = await api.post<ProjectMutationResponse<PageRenderResult>>(
                    withRevision(
                      `/api/projects/${projectId}/pages/${pageNumber}/render`,
                      saved.project.revision
                    )
                  );
                  // 生成されたページPNGを制作タブのプレビューへ反映する。
                  const renderedAdopted = await adoptMutationResponse(rendered);
                  if (!renderedAdopted.applied || !renderedAdopted.project) return;
                  applyRenderedPageAsset(renderedAdopted, pageNumber);
                  if (renderedAdopted.resynced) {
                    setMessage("構成が更新されたためページを再同期しました");
                    return;
                  }
                  setMessage("レイアウトを保存し、ページ画像を更新しました");
                } catch (error) {
                  // typed 409(revision競合等)は最新project採用＋派生状態再同期へ寄せる。
                  if (await handleProjectMutationError(error)) return;
                  setMessage(
                    `保存に失敗しました: ${error instanceof Error ? error.message : "不明なエラー"}`
                  );
                } finally {
                  setBusy(false);
                }
              }}
              onSuggest={async (family) => {
                if (!selected) return;
                const projectId = selected.id;
                const pageNumber = selectedPage;
                setBusy(true);
                try {
                  // 未保存の編集を先に保存する。再レイアウトAPIは保存済みManga JSONを基準に動くため、
                  // 保存しないと直前のコマ移動・吹き出し・overlay編集が失われる。
                  const saved = await adoptMutationResponse(
                    await putMangaJson(projectId, selected.revision, selected.manga_json)
                  );
                  if (!saved.applied || !saved.project) return;
                  const response = await api.post<ProjectMutationResponse<Schemas["LayoutSuggestResult"]>>(
                    withRevision(
                      `/api/projects/${projectId}/pages/${pageNumber}/layout/suggest`,
                      saved.project.revision
                    ),
                    { family }
                  );
                  const adopted = await adoptMutationResponse(response);
                  if (!adopted.applied || !adopted.project) return;
                  const { result } = adopted;
                  setMessage(`レイアウトを再提案しました（${result.layout_family}）`);
                } catch (error) {
                  if (await handleProjectMutationError(error)) return;
                  setMessage(
                    `再提案に失敗しました: ${error instanceof Error ? error.message : "不明なエラー"}`
                  );
                } finally {
                  setBusy(false);
                }
              }}
            />
          </Suspense>
        )}

        {activeTab === "knowledge" && <KnowledgePanel defaultWorkName={selected?.work_name ?? ""} />}

        {activeTab === "story" && selected && (
          <StoryPanel<Project>
            projectId={selected.id}
            revision={selected.revision}
            workName={selected.work_name}
            onProjectMutation={async (response) => {
              // App主要操作と同じく adoptMutationResponse を通し、stale snapshotは
              // 再同期済み project から派生状態を作り直す。未採用なら派生状態は触らない。
              const adopted = await adoptMutationResponse(response);
              if (adopted.applied && adopted.project) {
                await refreshProjectDerivedState(adopted.project);
              }
            }}
            onProjectMutationError={handleProjectMutationError}
            onBusyChange={(working, label) => {
              setBusy(working);
              setProgress(working ? { label, current: 0, total: 100, indeterminate: true } : null);
            }}
          />
        )}

        {activeTab === "production" && (
          <>
            <section className="status-band">
              <strong>{comfyStatus?.backend === "comfyui" ? "ComfyUI" : "stub"}</strong>
              <span>{comfyStatus?.message ?? "接続状態を確認中"}</span>
              <small>
                workflow: {comfyStatus?.workflow_exists ? "あり" : "なし"} / node:{" "}
                {comfyStatus?.workflow_valid ? "OK" : "未検証"}
              </small>
              {comfyStatus && comfyStatus.backend === "comfyui" && !comfyStatus.workflow_valid ? (
                <strong className="backend-warning">
                  警告: ComfyUIのworkflow設定エラーです。修正するまでコマ画像生成は失敗します。
                </strong>
              ) : comfyStatus && (comfyStatus.backend !== "comfyui" || !comfyStatus.connected) ? (
                <strong className="backend-warning">
                  警告: ComfyUIを使用していません。コマ画像はスタブになります。
                </strong>
              ) : null}
            </section>

            {productionStatus && (
              <section className={`production-band ${productionStatus.status}`}>
                <strong>
                  {productionStatus.status === "complete"
                    ? "制作完了"
                    : productionStatus.status === "ready"
                      ? "レンダリング待ち"
                      : "制作中"}
                </strong>
                <span>
                  採用 {productionStatus.adopted_panels}/{productionStatus.total_panels}コマ
                </span>
                <span>
                  ページ {productionStatus.rendered_pages}/{productionStatus.total_pages}
                </span>
                {(productionStatus.blockers ?? []).length > 0 && (
                  <details>
                    <summary>未完了 {(productionStatus.blockers ?? []).length}件</summary>
                    <ul>
                      {(productionStatus.blockers ?? []).map((blocker) => (
                        <li key={blocker}>{blocker}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {jobHistory.length > 0 && (
                  <details className="job-history">
                    <summary>生成履歴</summary>
                    <ul>
                      {jobHistory.slice(0, 10).map((job) => (
                        <li key={job.id}>
                          {job.panel_id}: {job.status} {job.progress}%
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </section>
            )}

            {selected && (
              <details className="common-prompts">
                <summary>共通プロンプト</summary>
                <div className="common-prompt-grid">
                  <label>
                    全コマ共通positive
                    <textarea
                      value={selected.manga_json.common_positive_prompt}
                      onChange={(event) =>
                        updateManga((manga) => {
                          manga.common_positive_prompt = event.target.value;
                        })
                      }
                      spellCheck={false}
                    />
                  </label>
                  <label>
                    全コマ共通negative
                    <textarea
                      value={selected.manga_json.common_negative_prompt}
                      onChange={(event) =>
                        updateManga((manga) => {
                          manga.common_negative_prompt = event.target.value;
                        })
                      }
                      spellCheck={false}
                    />
                  </label>
                  <div className="actions">
                    <button onClick={applyAnimePreviewDefaults} disabled={busy}>
                      Anima 3向け初期値
                    </button>
                    <button onClick={applyFourPageDraftSettings} disabled={busy}>
                      4ページ仮設定
                    </button>
                  </div>
                </div>
              </details>
            )}

            {selected && (
              <details className="advanced-settings">
                <summary>生成環境・ロケーション</summary>
                <section className="workflow-settings">
                  <div className="section-heading">
                    <h2>workflowプリセット</h2>
                    <button onClick={addWorkflowPreset} disabled={busy}>
                      追加
                    </button>
                  </div>
                  <label>
                    プロジェクト既定
                    <select
                      value={selected.manga_json.active_workflow_preset_id ?? ""}
                      onChange={(event) =>
                        updateManga((manga) => {
                          manga.active_workflow_preset_id = event.target.value || null;
                        })
                      }
                    >
                      <option value="">workflow設定を維持</option>
                      {selected.manga_json.workflow_presets.map((preset) => (
                        <option key={preset.id} value={preset.id}>
                          {preset.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="preset-list">
                    {selected.manga_json.workflow_presets.map((preset) => (
                      <article key={preset.id}>
                        <label>
                          名前
                          <input
                            value={preset.name}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.name = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          checkpointノード
                          <input
                            value={preset.checkpoint_node_id}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.checkpoint_node_id = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          checkpoint名
                          <input
                            value={preset.checkpoint_name}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.checkpoint_name = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          VAEノード
                          <input
                            value={preset.vae_node_id}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.vae_node_id = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          VAE名
                          <input
                            value={preset.vae_name}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.vae_name = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          samplerノード
                          <input
                            value={preset.sampler_node_id}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.sampler_node_id = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          sampler
                          <input
                            value={preset.sampler_name}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.sampler_name = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          scheduler
                          <input
                            value={preset.scheduler}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.scheduler = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          steps
                          <input
                            type="number"
                            value={preset.steps ?? ""}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.steps = event.target.value ? Number(event.target.value) : null;
                              })
                            }
                          />
                        </label>
                        <label>
                          CFG
                          <input
                            type="number"
                            step="0.1"
                            value={preset.cfg ?? ""}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.cfg = event.target.value ? Number(event.target.value) : null;
                              })
                            }
                          />
                        </label>
                        <label>
                          denoise
                          <input
                            type="number"
                            step="0.05"
                            min="0"
                            max="1"
                            value={preset.denoise ?? ""}
                            onChange={(event) =>
                              updateWorkflowPreset(preset.id, (item) => {
                                item.denoise = event.target.value ? Number(event.target.value) : null;
                              })
                            }
                          />
                        </label>
                      </article>
                    ))}
                  </div>
                </section>
                <section className="location-settings">
                  <div className="section-heading">
                    <h2>ロケーション</h2>
                    <button onClick={addLocation} disabled={busy}>
                      追加
                    </button>
                  </div>
                  <div className="location-list">
                    {selected.manga_json.locations.map((location) => (
                      <article key={location.id}>
                        <div className="character-title">
                          <strong>{location.display_name}</strong>
                          <small>{location.id}</small>
                        </div>
                        <label>
                          表示名
                          <input
                            value={location.display_name}
                            onChange={(event) =>
                              updateLocation(location.id, (item) => {
                                item.display_name = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          背景prompt
                          <textarea
                            value={location.prompt}
                            onChange={(event) =>
                              updateLocation(location.id, (item) => {
                                item.prompt = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          negative
                          <textarea
                            value={location.negative_prompt}
                            onChange={(event) =>
                              updateLocation(location.id, (item) => {
                                item.negative_prompt = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          LoadImageノード
                          <input
                            value={location.reference_load_node_id}
                            onChange={(event) =>
                              updateLocation(location.id, (item) => {
                                item.reference_load_node_id = event.target.value;
                              })
                            }
                          />
                        </label>
                        <label>
                          参照画像
                          <input
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            onChange={(event) => {
                              const file = event.target.files?.[0];
                              if (file) void uploadLocationImage(location.id, file);
                            }}
                          />
                        </label>
                        {location.reference_image_asset && (
                          <img
                            className="reference-image"
                            src={assetUrl(location.reference_image_asset)}
                            alt={`${location.display_name}参照`}
                          />
                        )}
                      </article>
                    ))}
                  </div>
                </section>
              </details>
            )}

            {selected && (
              <details className="character-profiles">
                <summary>キャラクタープロファイル（{selected.manga_json.characters.length}人）</summary>
                <div className="section-heading">
                  <h2>キャラクター設定</h2>
                  <button onClick={addCharacter} disabled={busy}>
                    キャラ追加
                  </button>
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
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.display_name = event.target.value;
                            })
                          }
                        />
                      </label>
                      <label>
                        trigger prompt
                        <input
                          value={character.trigger_prompt}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.trigger_prompt = event.target.value;
                            })
                          }
                        />
                      </label>
                      <label>
                        外見タグ
                        <textarea
                          value={character.appearance_prompt}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.appearance_prompt = event.target.value;
                            })
                          }
                          spellCheck={false}
                        />
                      </label>
                      <label>
                        衣装タグ
                        <textarea
                          value={character.outfit_prompt}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.outfit_prompt = event.target.value;
                            })
                          }
                          spellCheck={false}
                        />
                      </label>
                      <label>
                        個別negative
                        <input
                          value={character.negative_prompt}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.negative_prompt = event.target.value;
                            })
                          }
                        />
                      </label>
                      <label>
                        LoRAノードID
                        <input
                          value={character.lora_node_id}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.lora_node_id = event.target.value;
                            })
                          }
                        />
                      </label>
                      <label>
                        LoRA名
                        <input
                          value={character.lora_name}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.lora_name = event.target.value;
                            })
                          }
                        />
                      </label>
                      <label>
                        model強度
                        <input
                          type="number"
                          step="0.05"
                          min="-2"
                          max="2"
                          value={character.lora_strength_model}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.lora_strength_model = Number(event.target.value);
                            })
                          }
                        />
                      </label>
                      <label>
                        CLIP強度
                        <input
                          type="number"
                          step="0.05"
                          min="-2"
                          max="2"
                          value={character.lora_strength_clip}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.lora_strength_clip = Number(event.target.value);
                            })
                          }
                        />
                      </label>
                      <label>
                        参照画像ノードID
                        <input
                          value={character.reference_load_node_id}
                          onChange={(event) =>
                            updateCharacter(character.id, (item) => {
                              item.reference_load_node_id = event.target.value;
                            })
                          }
                        />
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
                        <img
                          className="reference-image"
                          src={assetUrl(character.reference_image_asset)}
                          alt={`${character.display_name}参照画像`}
                        />
                      )}
                    </article>
                  ))}
                </div>
              </details>
            )}

            <section className="content-grid">
              <div className="preview">
                <div className="page-workbench">
                  <div className={`tabs ${selected?.manga_json.reading_direction === "ltr" ? "" : "rtl"}`}>
                    {[...(selected?.manga_json.pages ?? [])]
                      .sort((a, b) =>
                        selected?.manga_json.reading_direction === "ltr" ? a.page - b.page : b.page - a.page
                      )
                      .map(({ page }) => (
                        <button
                          key={page}
                          className={selectedPage === page ? "active" : ""}
                          onClick={() => setSelectedPage(page)}
                        >
                          {page}p
                          {productionStatus?.pages.find((item) => item.page === page)?.status === "complete"
                            ? " 完了"
                            : ""}
                        </button>
                      ))}
                  </div>
                  <label className="incomplete-filter">
                    <input
                      type="checkbox"
                      checked={showIncompleteOnly}
                      onChange={(event) => setShowIncompleteOnly(event.target.checked)}
                    />
                    未完成のみ
                  </label>
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
                    {visiblePanels.map((panel) => (
                      <button
                        type="button"
                        key={panel.panel_id}
                        className={currentPanel?.panel_id === panel.panel_id ? "active-panel" : ""}
                        onClick={() => setSelectedPanelId(panel.panel_id)}
                      >
                        <strong>{panel.panel_id}</strong>
                        <span>{panel.shot}</span>
                        <small>
                          {panel.generation.backend} / {panel.generation.status}
                        </small>
                      </button>
                    ))}
                  </div>
                </div>
                {currentPanel && (
                  <div className="panel-editor">
                    <h2>選択中のコマ</h2>
                    <div className="panel-meta">
                      <strong>{currentPanel.panel_id}</strong>
                      <span>{currentPanel.generation.message || "生成メッセージなし"}</span>
                      {currentPanel.generation.prompt_id && (
                        <small>prompt_id: {currentPanel.generation.prompt_id}</small>
                      )}
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
                    {selected && (
                      <div className="settings-grid">
                        <label>
                          ロケーション
                          <select
                            value={currentPanel.location_id}
                            onChange={(event) =>
                              updateCurrentPanel((panel) => {
                                panel.location_id = event.target.value;
                              })
                            }
                          >
                            <option value="">指定なし</option>
                            {selected.manga_json.locations.map((location) => (
                              <option key={location.id} value={location.id}>
                                {location.display_name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          workflowプリセット
                          <select
                            value={currentPanel.generation.workflow_preset_id ?? ""}
                            onChange={(event) =>
                              updateCurrentPanel((panel) => {
                                panel.generation.workflow_preset_id = event.target.value || null;
                              })
                            }
                          >
                            <option value="">プロジェクト既定</option>
                            {selected.manga_json.workflow_presets.map((preset) => (
                              <option key={preset.id} value={preset.id}>
                                {preset.name}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                    )}
                    <details className="control-settings">
                      <summary>Control参照</summary>
                      <div className="control-grid">
                        {(["pose", "depth", "lineart", "background"] as const).map((kind) => {
                          const control = currentPanel.control_references.find((item) => item.kind === kind);
                          return (
                            <div key={kind}>
                              <strong>{kind}</strong>
                              <input
                                placeholder="LoadImageノードID"
                                value={control?.load_node_id ?? controlNodeDrafts[kind] ?? ""}
                                onChange={(event) => {
                                  const value = event.target.value;
                                  if (control)
                                    updateCurrentPanel((panel) => {
                                      const item = panel.control_references.find(
                                        (entry) => entry.id === control.id
                                      );
                                      if (item) item.load_node_id = value;
                                    });
                                  else setControlNodeDrafts((drafts) => ({ ...drafts, [kind]: value }));
                                }}
                              />
                              <input
                                type="file"
                                accept="image/png,image/jpeg,image/webp"
                                onChange={(event) => {
                                  const file = event.target.files?.[0];
                                  if (file) void uploadControlImage(kind, file);
                                }}
                              />
                              {control && <img src={assetUrl(control.asset)} alt={`${kind}参照`} />}
                            </div>
                          );
                        })}
                      </div>
                    </details>
                    <label>
                      positive prompt
                      <textarea
                        value={currentPanel.generation.prompt || currentPanel.prompt}
                        onChange={(event) =>
                          updateCurrentPanel((panel) => {
                            panel.generation.prompt = event.target.value;
                          })
                        }
                        spellCheck={false}
                      />
                    </label>
                    <label>
                      negative prompt
                      <input
                        value={currentPanel.generation.negative_prompt}
                        onChange={(event) =>
                          updateCurrentPanel((panel) => {
                            panel.generation.negative_prompt = event.target.value;
                          })
                        }
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
                        onChange={(event) =>
                          updateCurrentPanel((panel) => {
                            panel.generation.seed = Number(event.target.value);
                          })
                        }
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
                          onChange={(event) =>
                            updateCurrentPanel((panel) => {
                              panel.generation.width = event.target.value ? Number(event.target.value) : null;
                            })
                          }
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
                          onChange={(event) =>
                            updateCurrentPanel((panel) => {
                              panel.generation.height = event.target.value
                                ? Number(event.target.value)
                                : null;
                            })
                          }
                        />
                      </label>
                      <label>
                        配置
                        <select
                          value={currentPanel.generation.fit_mode}
                          onChange={(event) =>
                            updateCurrentPanel((panel) => {
                              panel.generation.fit_mode = event.target.value as "cover" | "contain";
                            })
                          }
                        >
                          <option value="cover">cover</option>
                          <option value="contain">contain</option>
                        </select>
                      </label>
                      <label>
                        crop基準
                        <select
                          value={currentPanel.generation.crop_anchor}
                          onChange={(event) =>
                            updateCurrentPanel((panel) => {
                              panel.generation.crop_anchor = event.target.value as
                                | "center"
                                | "top"
                                | "bottom"
                                | "left"
                                | "right";
                            })
                          }
                        >
                          <option value="center">center</option>
                          <option value="top">top</option>
                          <option value="bottom">bottom</option>
                          <option value="left">left</option>
                          <option value="right">right</option>
                        </select>
                        <span className="anchor-controls">
                          {(["top", "left", "center", "right", "bottom"] as const).map((anchor) => (
                            <button
                              key={anchor}
                              type="button"
                              title={`crop ${anchor}`}
                              className={currentPanel.generation.crop_anchor === anchor ? "active" : ""}
                              onClick={() =>
                                updateCurrentPanel((panel) => {
                                  panel.generation.crop_anchor = anchor;
                                })
                              }
                            >
                              {anchor === "top"
                                ? "↑"
                                : anchor === "bottom"
                                  ? "↓"
                                  : anchor === "left"
                                    ? "←"
                                    : anchor === "right"
                                      ? "→"
                                      : "•"}
                            </button>
                          ))}
                        </span>
                      </label>
                    </div>
                    <div className="dialogue-editor">
                      <h3>写植</h3>
                      <label>
                        台詞
                        <textarea
                          value={currentDialogue?.text ?? ""}
                          onChange={(event) =>
                            updateCurrentDialogue((dialogue) => {
                              dialogue.text = event.target.value;
                            })
                          }
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
                            max="96"
                            placeholder="プロジェクト既定"
                            value={currentDialogue?.font_size ?? ""}
                            onChange={(event) =>
                              updateCurrentDialogue((dialogue) => {
                                dialogue.font_size = event.target.value ? Number(event.target.value) : null;
                              })
                            }
                          />
                        </label>
                        <label>
                          最大行
                          <input
                            type="number"
                            min="1"
                            max="8"
                            value={currentDialogue?.max_lines ?? 3}
                            onChange={(event) =>
                              updateCurrentDialogue((dialogue) => {
                                dialogue.max_lines = Number(event.target.value);
                              })
                            }
                          />
                        </label>
                      </div>
                    </div>
                    {currentPanel.image_asset && (
                      <div className="panel-visual-editor">
                        <img
                          className="panel-image"
                          src={assetUrl(currentPanel.image_asset)}
                          alt={`${currentPanel.panel_id}画像`}
                        />
                        {currentDialogue && (
                          <div
                            className="balloon-overlay"
                            style={{
                              left: `${(currentDialogue.box?.[0] ?? 0.48) * 100}%`,
                              top: `${(currentDialogue.box?.[1] ?? 0.06) * 100}%`,
                              width: `${(currentDialogue.box?.[2] ?? 0.46) * 100}%`,
                              height: `${(currentDialogue.box?.[3] ?? 0.22) * 100}%`
                            }}
                            onPointerDown={(event) => startBalloonDrag(event, "move")}
                            onPointerMove={moveBalloon}
                            onPointerUp={() => {
                              dragState.current = null;
                            }}
                          >
                            <span>{currentDialogue.text}</span>
                            <i
                              onPointerDown={(event) => {
                                event.stopPropagation();
                                startBalloonDrag(event, "resize");
                              }}
                            />
                          </div>
                        )}
                      </div>
                    )}
                    <div className="candidate-header">
                      <h3>画像候補</h3>
                      <label>
                        一度に生成
                        <select
                          value={candidateCount}
                          onChange={(event) => setCandidateCount(Number(event.target.value))}
                          disabled={busy}
                        >
                          {[1, 2, 3, 4].map((count) => (
                            <option key={count} value={count}>
                              {count}件
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    {currentPanel.image_candidates.length > 0 ? (
                      <div className="candidate-gallery">
                        {currentPanel.image_candidates.map((candidate) => (
                          <article
                            key={candidate.id}
                            className={
                              currentPanel.selected_candidate_id === candidate.id ? "selected-candidate" : ""
                            }
                          >
                            <img src={assetUrl(candidate.asset)} alt={`seed ${candidate.seed}の候補`} />
                            <div>
                              <strong>seed {candidate.seed}</strong>
                              <small>
                                {candidate.backend} / {candidate.status}
                              </small>
                            </div>
                            <details>
                              <summary>生成条件</summary>
                              <small>{(candidate.characters ?? []).join(", ") || "キャラ指定なし"}</small>
                              {(candidate.loras ?? []).map((lora) => (
                                <small key={`${candidate.id}-${lora.node_id}`}>
                                  LoRA {lora.node_id}: {lora.lora_name}
                                </small>
                              ))}
                              {(candidate.reference_images ?? []).map((reference) => (
                                <small key={`${candidate.id}-${reference.node_id}`}>
                                  参照 {reference.node_id}: {reference.character_id}
                                </small>
                              ))}
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
                      <button onClick={generateCurrentPanelImage} disabled={busy}>
                        画像生成
                      </button>
                      <button onClick={generateCurrentPanelImage} disabled={busy}>
                        再生成
                      </button>
                      <button onClick={generateCurrentPageImages} disabled={busy}>
                        ページ内全コマ生成
                      </button>
                      <button onClick={generateAllPageImages} disabled={busy}>
                        全ページ生成
                      </button>
                      <button onClick={renderCurrentPanelPage} disabled={busy}>
                        写植更新
                      </button>
                      <button onClick={renderCurrentPanelPage} disabled={busy}>
                        ページ更新
                      </button>
                      <button onClick={useStubForCurrentPanel} disabled={busy}>
                        stubへ戻す
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </section>
            <details className="json-pane">
              <summary>Manga JSON</summary>
              <textarea
                value={jsonText}
                onChange={(event) => setJsonText(event.target.value)}
                spellCheck={false}
              />
            </details>
          </>
        )}
      </main>
      {newProjectOpen && (
        <div
          className="modal-backdrop"
          role="button"
          tabIndex={0}
          aria-label="ダイアログを閉じる"
          onKeyDown={(event) => {
            if (event.key === "Escape") setNewProjectOpen(false);
          }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setNewProjectOpen(false);
          }}
        >
          <form
            className="project-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="new-project-heading"
            onSubmit={createProject}
          >
            <div className="dialog-heading">
              <h2 id="new-project-heading">新しい本</h2>
              <button
                type="button"
                className="icon-button"
                title="閉じる"
                onClick={() => setNewProjectOpen(false)}
              >
                <X size={18} />
              </button>
            </div>
            <label>
              タイトル
              <input
                value={newProjectTitle}
                onChange={(event) => setNewProjectTitle(event.target.value)}
                maxLength={120}
              />
            </label>
            <div className="actions dialog-actions">
              <button type="button" onClick={() => setNewProjectOpen(false)}>
                キャンセル
              </button>
              <button className="primary" disabled={busy || !newProjectTitle.trim()}>
                <Plus size={17} />
                作成
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
