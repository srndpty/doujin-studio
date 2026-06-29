import type { components } from "./api/schema";
import type { ProjectMutationResponse as ApiProjectMutationResponse } from "./api/types";

// OpenAPIスキーマを唯一の正とする。座標タプル(bbox/box)を含む型はOpenAPIが
// 固定長タプルを表現できないため、エディタ/ジオメトリ側の都合で手書きを維持する。
type Schemas = components["schemas"];

export type Dialogue = {
  speaker: string;
  text: string;
  kind?: string;
  balloon: string;
  // falseなら手動指定したballoonを保持。kind変更で自動追従させたい場合はtrueにする。
  balloon_auto?: boolean;
  position: string;
  on_screen?: boolean;
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
  // ページ座標(0..1、裁ち落とし用に-0.05..1.05)のコマ枠ポリゴン。レイアウトの正本。
  frame_points?: [number, number][] | null;
  // コマの重なり順（大きいほど手前）。
  z_index?: number;
  frame_role?: "normal" | "background" | "bleed" | "overlap" | "vertical_splash" | "cut_in";
  frame_source?: "auto" | "manual";
  shot: string;
  subject_mode?: "character_scene" | "reaction" | "prop_insert" | "hand_insert" | "background";
  role?: string;
  emotion?: string;
  background_density?: string;
  composition_notes?: string;
  text_safe_area?: [number, number, number, number] | null;
  emphasis?: number;
  camera: string;
  location_id: string;
  characters: string[];
  character_layout?: {
    id: string;
    position: "upper_left" | "upper_right" | "lower_left" | "lower_right" | "center";
    expression: string;
    action: string;
    region_box?: [number, number, number, number] | null;
  }[];
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

export type LoRABinding = Schemas["LoRABinding"];
export type ReferenceImageBinding = Schemas["ReferenceImageBinding"];
export type PanelControlReference = Schemas["PanelControlReference"];
export type RegionalWorkflowBinding = Schemas["RegionalWorkflowBinding"];
export type WorkflowPreset = Schemas["WorkflowPreset"];
export type LocationProfile = Schemas["LocationProfile"];

export type ImageCandidate = Schemas["ImageCandidate"];

export type Character = Schemas["Character"];

export type GenerationJob = Schemas["GenerationJobResponse"];

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
  page_goal?: string;
  emotional_curve?: string[];
  quality_notes?: string[];
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

export type ProductionStatus = Schemas["ProjectProductionStatus"];

export type ProjectSummary = Schemas["ProjectSummary"];

export type ComfyUIStatus = Schemas["ComfyUIStatusResponse"];

export type EmptyMutationResult = Schemas["EmptyMutationResult"];
export type PageRenderResult = Schemas["PageRenderResult"];
export type PanelPageRenderResult = Schemas["PanelPageRenderResult"];
export type BatchGenerationJobResult = Schemas["BatchGenerationJobResult"];
export type FolderExportResult = Schemas["FolderExportResult"];
export type PreflightFixResult = Schemas["PreflightFixResult"];
export type ReferenceAssetResult = Schemas["ReferenceAssetResult"];
export type ProjectDeletionResponse = Schemas["ProjectDeletionResponse"];
export type LayoutSuggestResult = Schemas["LayoutSuggestResult"];

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
