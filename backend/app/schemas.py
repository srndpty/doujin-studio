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


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    role: str = ""
    speech_style: str = ""
    visual_notes: str = ""


class MangaProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    work_name: str = ""
    premise: str = ""
    target_pages: int = 4
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


class ExportResponse(BaseModel):
    project_id: str
    cbz_asset: str
