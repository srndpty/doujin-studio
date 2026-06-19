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
    character_id: str


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


class MangaProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    work_name: str = ""
    premise: str = ""
    target_pages: int = 4
    common_positive_prompt: str = ""
    common_negative_prompt: str = ""
    characters: list[Character] = Field(default_factory=list)
    pages: list[Page] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    work_name: str = Field(default="", max_length=120)


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


class ExportResponse(BaseModel):
    project_id: str
    cbz_asset: str
    warnings: list[str] = Field(default_factory=list)
