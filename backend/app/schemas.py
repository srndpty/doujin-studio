from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Dialogue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str
    text: str = Field(min_length=1, max_length=120)
    balloon: Literal["round", "thought", "shout"] = "round"
    position: Literal["upper_left", "upper_right", "lower_left", "lower_right", "center"] = "upper_right"
    box: tuple[float, float, float, float] | None = None
    font_size: int = Field(default=24, ge=10, le=72)
    max_lines: int = Field(default=3, ge=1, le=8)

    @field_validator("box")
    @classmethod
    def validate_box(cls, value: tuple[float, float, float, float] | None) -> tuple[float, float, float, float] | None:
        if value is None:
            return value
        left, top, width, height = value
        if min(value) < 0 or left + width > 1 or top + height > 1:
            raise ValueError("boxは0から1の範囲に収めてください")
        if width <= 0 or height <= 0:
            raise ValueError("boxの幅と高さは正の値にしてください")
        return value


class Sfx(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=40)
    position: Literal["upper_left", "upper_right", "lower_left", "lower_right", "center"] = "center"
    style: str = "small_handwritten"


class WorkflowPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    checkpoint_node_id: str = ""
    checkpoint_name: str = ""
    vae_node_id: str = ""
    vae_name: str = ""
    sampler_node_id: str = ""
    sampler_name: str = ""
    scheduler: str = ""
    steps: int | None = Field(default=None, ge=1, le=200)
    cfg: float | None = Field(default=None, ge=0, le=30)
    denoise: float | None = Field(default=None, ge=0, le=1)


class PanelControlReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["pose", "depth", "lineart", "background"] = "pose"
    asset: str
    load_node_id: str


class LoRABinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    lora_name: str
    strength_model: float = Field(default=1.0, ge=-2.0, le=2.0)
    strength_clip: float = Field(default=1.0, ge=-2.0, le=2.0)


class ReferenceImageBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    asset: str
    character_id: str = ""
    kind: Literal["character", "location", "pose", "depth", "lineart", "background"] = "character"


class GenerationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["stub", "comfyui"] = "stub"
    prompt: str = ""
    negative_prompt: str = ""
    seed: int = Field(default=1, ge=0)
    workflow_id: str | None = None
    prompt_id: str | None = None
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    fit_mode: Literal["cover", "contain"] = "cover"
    crop_anchor: Literal["center", "top", "bottom", "left", "right"] = "center"
    text_policy: Literal["no_text"] = "no_text"
    model_notes: str = ""
    status: Literal["pending", "running", "queued", "done", "fallback", "skipped", "error"] = "pending"
    message: str = ""
    loras: list[LoRABinding] = Field(default_factory=list)
    reference_images: list[ReferenceImageBinding] = Field(default_factory=list)
    workflow_preset_id: str | None = None
    workflow_preset: WorkflowPreset | None = None


class ImageCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    asset: str
    backend: Literal["stub", "comfyui"]
    status: Literal["done", "fallback", "error"]
    prompt: str = ""
    negative_prompt: str = ""
    characters: list[str] = Field(default_factory=list)
    loras: list[LoRABinding] = Field(default_factory=list)
    reference_images: list[ReferenceImageBinding] = Field(default_factory=list)
    workflow_preset: WorkflowPreset | None = None
    seed: int = Field(ge=0)
    prompt_id: str | None = None
    message: str = ""
    created_at: datetime


class Panel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    panel_id: str
    bbox: tuple[float, float, float, float]
    shot: str
    camera: str = ""
    location_id: str = ""
    characters: list[str] = Field(default_factory=list)
    prompt: str = ""
    image_asset: str | None = None
    image_candidates: list[ImageCandidate] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    control_references: list[PanelControlReference] = Field(default_factory=list)
    dialogue: list[Dialogue] = Field(default_factory=list)
    sfx: list[Sfx] = Field(default_factory=list)
    generation: GenerationInfo = Field(default_factory=GenerationInfo)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        left, top, width, height = value
        if min(value) < 0 or left + width > 1 or top + height > 1:
            raise ValueError("bboxは0から1の範囲に収めてください")
        if width <= 0 or height <= 0:
            raise ValueError("bboxの幅と高さは正の値にしてください")
        return value


class Page(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    theme: str
    layout_template: str
    panels: list[Panel] = Field(min_length=1)
    render_status: Literal["pending", "done"] = "pending"
    rendered_at: datetime | None = None


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    role: str = ""
    speech_style: str = ""
    visual_notes: str = ""
    trigger_prompt: str = ""
    appearance_prompt: str = ""
    outfit_prompt: str = ""
    negative_prompt: str = ""
    lora_node_id: str = ""
    lora_name: str = ""
    lora_strength_model: float = Field(default=1.0, ge=-2.0, le=2.0)
    lora_strength_clip: float = Field(default=1.0, ge=-2.0, le=2.0)
    reference_image_asset: str | None = None
    reference_load_node_id: str = ""


class LocationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    prompt: str = ""
    negative_prompt: str = ""
    reference_image_asset: str | None = None
    reference_load_node_id: str = ""


class MangaProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    work_name: str = ""
    premise: str = ""
    target_pages: int = 4
    common_positive_prompt: str = ""
    common_negative_prompt: str = ""
    characters: list[Character] = Field(default_factory=list)
    locations: list[LocationProfile] = Field(default_factory=list)
    workflow_presets: list[WorkflowPreset] = Field(default_factory=list)
    active_workflow_preset_id: str | None = None
    pages: list[Page] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    work_name: str = Field(default="", max_length=120)
    target_pages: Literal[4, 8, 16] = 4


class ProjectSummary(BaseModel):
    id: str
    title: str
    work_name: str
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectSummary):
    manga_json: MangaProject


class GenerateNameRequest(BaseModel):
    work_name: str = Field(min_length=1, max_length=120)
    character_a: str = Field(min_length=1, max_length=80)
    character_b: str = Field(min_length=1, max_length=80)
    situation: str = Field(min_length=1, max_length=200)
    ending_direction: str = Field(min_length=1, max_length=200)
    target_pages: Literal[4, 8, 16] = 4


class RenderResponse(BaseModel):
    project_id: str
    page_assets: list[str]
    manga_json: MangaProject


class RenderRequest(BaseModel):
    force: bool = False


class ComfyUIStatusResponse(BaseModel):
    backend: str
    base_url: str
    workflow_path: str
    connected: bool
    workflow_exists: bool
    workflow_valid: bool
    missing_nodes: list[str] = Field(default_factory=list)
    message: str


class PanelImageGenerationResponse(BaseModel):
    project_id: str
    panel_id: str
    manga_json: MangaProject


class PanelPageRenderResponse(BaseModel):
    project_id: str
    panel_id: str
    page_asset: str
    manga_json: MangaProject


class GenerationJobCreate(BaseModel):
    candidate_count: int = Field(default=1, ge=1, le=4)


class BatchGenerationJobCreate(BaseModel):
    page: int | None = Field(default=None, ge=1)
    candidate_count: int = Field(default=1, ge=1, le=4)


class GenerationJobResponse(BaseModel):
    id: str
    project_id: str
    panel_id: str
    status: Literal["queued", "running", "done", "error", "cancelled"]
    progress: int = Field(ge=0, le=100)
    current: int = Field(ge=0)
    total: int = Field(ge=0)
    node: str | None = None
    message: str
    candidate_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PromptPreviewResponse(BaseModel):
    panel_id: str
    positive_prompt: str
    negative_prompt: str
    character_ids: list[str] = Field(default_factory=list)


class BatchGenerationJobResponse(BaseModel):
    jobs: list[GenerationJobResponse]


class PageProductionStatus(BaseModel):
    page: int
    status: Literal["incomplete", "ready", "complete"]
    adopted_panels: int
    total_panels: int
    rendered: bool
    blockers: list[str] = Field(default_factory=list)


class ProjectProductionStatus(BaseModel):
    project_id: str
    status: Literal["incomplete", "ready", "complete"]
    adopted_panels: int
    total_panels: int
    rendered_pages: int
    total_pages: int
    pages: list[PageProductionStatus]
    blockers: list[str] = Field(default_factory=list)


class CharacterReferenceResponse(BaseModel):
    character_id: str
    asset: str
    manga_json: MangaProject


class ReferenceAssetResponse(BaseModel):
    target_id: str
    asset: str
    manga_json: MangaProject


class ExportResponse(BaseModel):
    project_id: str
    cbz_asset: str
    warnings: list[str] = Field(default_factory=list)


# --- LLM ---

class LLMStatusResponse(BaseModel):
    provider: str
    base_url: str
    model: str
    connected: bool
    available_models: list[str] = Field(default_factory=list)
    message: str


# --- 作品知識DB ---

DocType = Literal["json", "markdown", "txt"]
KnowledgeUsage = Literal["required", "reference"]


class KnowledgeFile(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=2_000_000)


class KnowledgeImportRequest(BaseModel):
    work_name: str = Field(min_length=1, max_length=120)
    usage: KnowledgeUsage = "reference"
    files: list[KnowledgeFile] = Field(min_length=1)


class KnowledgeDocumentRequest(BaseModel):
    work_name: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=200)
    doc_type: DocType = "txt"
    usage: KnowledgeUsage = "reference"
    content: str = Field(min_length=1, max_length=2_000_000)


class KnowledgeChunkResponse(BaseModel):
    id: str
    source_id: str
    work_name: str
    usage: KnowledgeUsage
    kind: str = ""
    title: str = ""
    content: str = ""
    policy: str = ""
    tags: list[str] = Field(default_factory=list)
    position: int = 0


class KnowledgeSourceResponse(BaseModel):
    id: str
    work_name: str
    title: str
    doc_type: DocType
    usage: KnowledgeUsage
    chunk_count: int
    created_at: datetime


class KnowledgeImportResponse(BaseModel):
    sources: list[KnowledgeSourceResponse]


class KnowledgeSearchRequest(BaseModel):
    work_name: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=200)
    usage: KnowledgeUsage | None = None
    limit: int = Field(default=10, ge=1, le=50)


class KnowledgeSearchHit(BaseModel):
    chunk: KnowledgeChunkResponse
    score: float
    method: Literal["trigram", "like"]


class KnowledgeSearchResponse(BaseModel):
    hits: list[KnowledgeSearchHit]


# --- 段階生成のステージデータ ---

class BriefCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    role: str = ""


class BriefStage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    synopsis: str
    tone: str = ""
    characters: list[BriefCharacter] = Field(default_factory=list)
    canon_conditions: list[str] = Field(default_factory=list)


class CharacterArc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arc: str = ""


class PlotStage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ki: str = ""
    sho: str = ""
    ten: str = ""
    ketsu: str = ""
    beats: list[str] = Field(default_factory=list)
    character_arcs: list[CharacterArc] = Field(default_factory=list)


class PageOutline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(ge=1)
    purpose: str = ""
    setting: str = ""
    characters: list[str] = Field(default_factory=list)
    hook: str = ""


class PagesStage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages: list[PageOutline] = Field(min_length=1)


class ScriptDialogue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speaker: str = ""
    text: str = ""


class ScriptPanel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shot: str = ""
    camera: str = ""
    location: str = ""
    visual_prompt: str = ""
    dialogue: list[ScriptDialogue] = Field(default_factory=list)
    sfx: list[str] = Field(default_factory=list)


class ScriptPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(ge=1)
    layout: str = ""
    panels: list[ScriptPanel] = Field(min_length=1, max_length=4)


class ScriptStage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages: list[ScriptPage] = Field(min_length=1)


StoryStageName = Literal["brief", "plot", "pages", "script"]
StageStatus = Literal["empty", "draft", "approved"]


class StoryStageState(BaseModel):
    status: StageStatus = "empty"
    data: dict | None = None
    knowledge_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    updated_at: datetime | None = None


class StorySessionCreate(BaseModel):
    work_name: str = Field(default="", max_length=120)
    target_pages: Literal[4, 8, 16] = 4
    instruction: str = Field(default="", max_length=2000)


class StorySessionResponse(BaseModel):
    id: str
    project_id: str
    work_name: str
    target_pages: int
    instruction: str
    stages: dict[str, StoryStageState]
    created_at: datetime
    updated_at: datetime


class StorySessionSummary(BaseModel):
    id: str
    project_id: str
    work_name: str
    target_pages: int
    instruction: str
    created_at: datetime
    updated_at: datetime


class StageGenerateRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)


class StageUpdateRequest(BaseModel):
    data: dict


class ProjectRevisionResponse(BaseModel):
    id: str
    project_id: str
    label: str
    created_at: datetime
