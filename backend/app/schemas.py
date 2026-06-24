from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 旧balloon値から新しい吹き出し種別への対応表。
BALLOON_MIGRATION = {"round": "oval", "thought": "cloud", "shout": "burst"}
BalloonKind = Literal["oval", "cloud", "burst", "caption", "none"]
PositionAnchor = Literal["upper_left", "upper_right", "lower_left", "lower_right", "center"]


class BalloonTail(BaseModel):
    """話者方向を示す吹き出しの先端（しっぽ）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # 先端の座標（コマ基準 0..1）。話者の口元へ向ける。
    tip: tuple[float, float] = (0.5, 1.0)
    # 付け根の位置（吹き出しの辺に沿った 0..1）。
    base: float = Field(default=0.5, ge=0.0, le=1.0)
    # 付け根の幅（吹き出し幅に対する比率）。
    width: float = Field(default=0.16, ge=0.02, le=0.6)

    @field_validator("tip")
    @classmethod
    def validate_tip(cls, value: tuple[float, float]) -> tuple[float, float]:
        x, y = value
        if not (-0.2 <= x <= 1.2 and -0.2 <= y <= 1.2):
            raise ValueError("tipはコマ付近(-0.2〜1.2)に収めてください")
        return value


class Dialogue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str
    text: str = Field(min_length=1, max_length=400)
    balloon: BalloonKind = "oval"
    position: PositionAnchor = "upper_right"
    box: tuple[float, float, float, float] | None = None
    # 縦書きを既定にする（日本語漫画）。
    vertical: bool = True
    font_size: int | None = Field(default=None, ge=10, le=96)
    min_font_size: int | None = Field(default=None, ge=8, le=96)
    max_lines: int = Field(default=6, ge=1, le=20)
    tail: BalloonTail | None = None

    @field_validator("balloon", mode="before")
    @classmethod
    def migrate_balloon(cls, value):
        if isinstance(value, str):
            return BALLOON_MIGRATION.get(value, value)
        return value

    @field_validator("box")
    @classmethod
    def validate_box(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
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
    position: PositionAnchor = "center"
    style: str = "small_handwritten"
    # コマ基準の配置座標（中心 0..1）。未指定時はpositionから決める。
    box: tuple[float, float] | None = None
    font_size: int = Field(default=54, ge=12, le=240)
    rotation: float = Field(default=0.0, ge=-180.0, le=180.0)
    color: str = "#191919"
    outline_color: str = "#FFFFFF"
    outline_width: int = Field(default=4, ge=0, le=24)
    vertical: bool = False
    # below=コマ画像の上だが吹き出しの下、above=最前面。
    layer: Literal["below", "above"] = "above"

    @field_validator("box")
    @classmethod
    def validate_box(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is None:
            return value
        x, y = value
        if not (-0.2 <= x <= 1.2 and -0.2 <= y <= 1.2):
            raise ValueError("boxはコマ付近(-0.2〜1.2)に収めてください")
        return value


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
    # panel表示を更新する権利を持つactive job。遅延した旧jobの後着更新を拒否する。
    active_job_id: str | None = None
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    fit_mode: Literal["cover", "contain"] = "cover"
    crop_anchor: Literal["center", "top", "bottom", "left", "right"] = "center"
    # パン・ズーム方式のcrop。anchorを粗い基準、offsetを微調整として合成する。
    crop_scale: float = Field(default=1.0, ge=1.0, le=4.0)
    crop_offset_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    crop_offset_y: float = Field(default=0.0, ge=-1.0, le=1.0)
    # 注視点（0..1）。指定時はoffsetより優先してこの点を中心に寄せる。
    focal_x: float | None = Field(default=None, ge=0.0, le=1.0)
    focal_y: float | None = Field(default=None, ge=0.0, le=1.0)
    text_policy: Literal["no_text"] = "no_text"
    model_notes: str = ""
    status: Literal["pending", "running", "queued", "done", "fallback", "skipped", "error"] = (
        "pending"
    )
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
    # bbox内の相対座標で表す変形コマ。未指定時は長方形。
    shape_points: list[tuple[float, float]] | None = None
    shot: str
    # コマの主題。prop_insert/hand_insert/backgroundではキャラ同一性を抑制する。
    subject_mode: Literal[
        "character_scene", "reaction", "prop_insert", "hand_insert", "background"
    ] = "character_scene"
    # 構図段階の役割（establish/dialogue/reveal/action/punchline/silent/montage等）。
    role: str = ""
    # 強調度（1=控えめ、5=見せ場）。レイアウトエンジンが面積配分に使う。
    emphasis: int = Field(default=2, ge=1, le=5)
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
    def validate_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        left, top, width, height = value
        if min(value) < 0 or left + width > 1 or top + height > 1:
            raise ValueError("bboxは0から1の範囲に収めてください")
        if width <= 0 or height <= 0:
            raise ValueError("bboxの幅と高さは正の値にしてください")
        return value

    @field_validator("shape_points")
    @classmethod
    def validate_shape_points(
        cls, value: list[tuple[float, float]] | None
    ) -> list[tuple[float, float]] | None:
        if value is None:
            return None
        if not 3 <= len(value) <= 12:
            raise ValueError("shape_pointsは3〜12点で指定してください")
        if any(x < 0 or x > 1 or y < 0 or y > 1 for x, y in value):
            raise ValueError("shape_pointsはbbox内の0から1で指定してください")
        return value


class OverlayElement(BaseModel):
    """コマ枠外へ配置する演出レイヤー（オーバーフレーム）。

    透過画像やマスク付き人物を、ページ座標で前面/背面に重ねる。
    occluded_by_panel_idsに挙げたコマの絵だけは手前に再描画され、
    「上のコマには隠れ、中央以降で手前へ出る」表現を実現する。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    # 抽出元コマ（人物の出所）。読み順判定にも使う。
    source_panel_id: str = ""
    # 透過画像（人物切り抜き等）。
    asset: str | None = None
    # 別途マスク画像を持つ場合のアルファ。
    mask_asset: str | None = None
    # ページ全体を基準にした配置(x, y, width, height) 0..1。
    box: tuple[float, float, float, float]
    scale: float = Field(default=1.0, ge=0.05, le=4.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # back=通常コマの直後（背面）、front=コマより手前。
    layer: Literal["back", "front"] = "front"
    z_index: int = 0
    # frontでもこのコマの絵には隠れる（手前に再描画される）。
    occluded_by_panel_ids: list[str] = Field(default_factory=list)

    @field_validator("box")
    @classmethod
    def validate_box(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        left, top, width, height = value
        if width <= 0 or height <= 0:
            raise ValueError("overlayのboxは正の幅・高さにしてください")
        return value


class Page(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    theme: str
    layout_template: str
    # レイアウトエンジンが選んだテンプレートファミリー。
    layout_family: str = ""
    # ページの演出意図（構図段階）。
    intent: str = ""
    # 手動編集後はtrue。自動レイアウト再提案で上書きしない。
    layout_locked: bool = False
    # コマの読み順（panel_id列）。右上から左下を既定とする。
    reading_order: list[str] = Field(default_factory=list)
    overlay_elements: list[OverlayElement] = Field(default_factory=list)
    panels: list[Panel] = Field(min_length=1)
    render_status: Literal["pending", "done"] = "pending"
    rendered_at: datetime | None = None
    # 描画入力hashを含む不変PNG。古い描画が新しい描画を上書きしないための正本。
    render_asset: str | None = None
    render_hash: str | None = None


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
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


class TypographySettings(BaseModel):
    """写植の既定設定。"""

    model_config = ConfigDict(extra="forbid")

    # 優先フォント名（源暎アンチック）。未導入時はBIZ UDゴシックへ退避する。
    primary_font: str = "源暎アンチック"
    default_font_size: int = Field(default=34, ge=10, le=96)
    min_font_size: int = Field(default=26, ge=8, le=96)
    vertical_default: bool = True


class PageLayoutSettings(BaseModel):
    """ページ余白とコマ間余白を別々に持つ。"""

    model_config = ConfigDict(extra="forbid")

    # 外周余白（ページ端からの余白）。
    outer_margin: float = Field(default=0.04, ge=0.0, le=0.2)
    # コマ間余白（ガター）。約1.0〜1.2%。
    gutter: float = Field(default=0.012, ge=0.0, le=0.1)


class MangaProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    work_name: str = ""
    premise: str = ""
    target_pages: int = 4
    # 読み方向。日本漫画は右綴じ・右から左を既定にする。
    reading_direction: Literal["rtl", "ltr"] = "rtl"
    typography: TypographySettings = Field(default_factory=TypographySettings)
    page_layout: PageLayoutSettings = Field(default_factory=PageLayoutSettings)
    common_positive_prompt: str = ""
    common_negative_prompt: str = ""
    characters: list[Character] = Field(default_factory=list)
    locations: list[LocationProfile] = Field(default_factory=list)
    workflow_presets: list[WorkflowPreset] = Field(default_factory=list)
    active_workflow_preset_id: str | None = None
    pages: list[Page] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_consistency(self) -> "MangaProject":
        """保存不能な構造破綻のみを弾く。

        参照切れ・読み順・occlusion・target_pages不一致など「制作上の警告」は
        preflight側が担当する（savableなままユーザーに気づかせる）。ここでは
        コマID/ページ番号の一意性やworkflow_preset参照のような、後段のlookupが
        破綻して原因不明の不具合になる種類の不整合だけをエラーにする。
        """
        errors: list[str] = []
        preset_id_list = [preset.id for preset in self.workflow_presets]
        preset_ids = set(preset_id_list)
        # preset ID重複はpreset_ids(集合)を通り抜け、解決時に先頭が選ばれて意味が曖昧になる。
        if len(preset_id_list) != len(preset_ids):
            errors.append("workflow preset IDが重複しています")

        page_numbers = [page.page for page in self.pages]
        if len(page_numbers) != len(set(page_numbers)):
            errors.append("ページ番号が重複しています")

        if self.active_workflow_preset_id and self.active_workflow_preset_id not in preset_ids:
            errors.append(
                f"active_workflow_preset_idが存在しません: {self.active_workflow_preset_id}"
            )

        all_panel_ids: list[str] = []
        for page in self.pages:
            panel_ids = [panel.panel_id for panel in page.panels]
            all_panel_ids.extend(panel_ids)
            if len(panel_ids) != len(set(panel_ids)):
                errors.append(f"{page.page}ページでコマIDが重複しています")
            for panel in page.panels:
                preset_id = panel.generation.workflow_preset_id
                if preset_id and preset_id not in preset_ids:
                    errors.append(f"{panel.panel_id}が存在しないworkflow_presetを参照: {preset_id}")

        if len(all_panel_ids) != len(set(all_panel_ids)):
            errors.append("コマIDがページ間で重複しています")

        if errors:
            raise ValueError("; ".join(errors))
        return self


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    work_name: str = Field(default="", max_length=120)
    target_pages: Literal[4, 8, 16] = 4


class ProjectSummary(BaseModel):
    id: str
    title: str
    work_name: str
    # manga_jsonの楽観ロック用バージョン。PUT時にこの値を添えて競合を検出する。
    revision: int = 0
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectSummary):
    manga_json: MangaProject


MutationResultT = TypeVar("MutationResultT")


class ProjectMutationResponse(BaseModel, Generic[MutationResultT]):
    """ProjectRecordを変更するAPIの共通成功応答。

    projectは「その操作が確定した時点のsnapshot」で、page assetとmanga_jsonの整合を守る。
    応答整形より前に別更新が入ると、projectのrevisionはDB最新より古くなり得る。その場合
    ``latest_revision`` が ``project.revision`` を上回るので、フロントは操作結果を反映した後に
    最新stateへ再同期できる。resultは操作固有情報だけを持つ。
    """

    project: ProjectDetail
    # 応答整形時点でのDB上の最新revision。project.revisionと等しければ追従不要、
    # 上回っていればフロントはreloadで最新へ再同期する。
    latest_revision: int
    result: MutationResultT


class EmptyMutationResult(BaseModel):
    """操作固有情報がないmutation用の空result。"""


class GenerateNameRequest(BaseModel):
    work_name: str = Field(min_length=1, max_length=120)
    character_a: str = Field(min_length=1, max_length=80)
    character_b: str = Field(min_length=1, max_length=80)
    situation: str = Field(min_length=1, max_length=200)
    ending_direction: str = Field(min_length=1, max_length=200)
    target_pages: Literal[4, 8, 16] = 4


class ApiErrorResponse(BaseModel):
    """FastAPI標準のエラー本体({"detail": ...})に対応する型。

    409(キャンセル)・502(生成バックエンド失敗)などの同期API契約をOpenAPIへ明示するため。
    """

    detail: str


class ProjectRevisionConflictResponse(ApiErrorResponse):
    """revision競合時の専用409応答。最新projectを同梱する。"""

    code: Literal["project_revision_conflict"] = "project_revision_conflict"
    expected_revision: int
    actual_revision: int
    project: ProjectDetail


class ProjectMutationPartialSuccessResponse(ApiErrorResponse):
    """複合操作の前段だけが確定した部分成功の専用409応答。

    部分成功は同時更新で起きるため通常成功よりstaleになりやすい。``completed_project`` は
    前段(候補採用など)を確定した時点のsnapshot、``project`` は応答整形時点のDB最新stateで、
    両者を分けて返す。フロントは``project``を採用し、``latest_revision``が更にそれを上回る場合は
    最新stateへ再同期する。``completed_project``は「前段が何を確定したか」を示すための参考値。
    """

    code: Literal["project_mutation_partially_applied"] = "project_mutation_partially_applied"
    completed_operation: str
    failed_operation: str
    completed_project: ProjectDetail
    project: ProjectDetail
    latest_revision: int


class ProjectRenderResult(BaseModel):
    page_assets: list[str]
    warnings: list[str] = Field(default_factory=list)


class FontInfo(BaseModel):
    id: str
    name: str
    path: str
    available: bool
    is_primary: bool = False


class FontsResponse(BaseModel):
    dialogue_font: str
    dialogue_font_available: bool
    fonts: list[FontInfo]


class RenderRequest(BaseModel):
    force: bool = False


class PreflightIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    page: int
    panel_id: str | None = None


class PreflightResponse(BaseModel):
    project_id: str
    page: int | None = None
    ok: bool
    errors: list[PreflightIssue] = Field(default_factory=list)
    warnings: list[PreflightIssue] = Field(default_factory=list)


class PageRenderResult(BaseModel):
    page: int
    page_asset: str
    warnings: list[str] = Field(default_factory=list)
    preflight: PreflightResponse


class LayoutSuggestRequest(BaseModel):
    family: str | None = None


class LayoutSuggestResult(BaseModel):
    page: int
    layout_family: str


class ComfyUIStatusResponse(BaseModel):
    backend: str
    base_url: str
    workflow_path: str
    connected: bool
    workflow_exists: bool
    workflow_valid: bool
    missing_nodes: list[str] = Field(default_factory=list)
    message: str


class PanelImageGenerationResult(BaseModel):
    panel_id: str


class PanelPageRenderResult(BaseModel):
    panel_id: str
    page_asset: str
    warnings: list[str] = Field(default_factory=list)


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
    generation_input_hash: str | None = None
    created_at: datetime
    updated_at: datetime


class PromptPreviewResponse(BaseModel):
    panel_id: str
    positive_prompt: str
    negative_prompt: str
    character_ids: list[str] = Field(default_factory=list)


class BatchGenerationJobResult(BaseModel):
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


class CharacterReferenceResult(BaseModel):
    character_id: str
    asset: str


class ReferenceAssetResult(BaseModel):
    target_id: str
    asset: str


class ExportResult(BaseModel):
    cbz_asset: str
    absolute_path: str
    warnings: list[str] = Field(default_factory=list)


class OpenExportFolderResponse(BaseModel):
    project_id: str
    folder_path: str
    cbz_path: str
    cbz_exists: bool


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


class LocalKnowledgeWorkResponse(BaseModel):
    work_id: str
    work_name: str
    description: str = ""
    document_count: int = 0


class LocalKnowledgeSyncResponse(BaseModel):
    work: LocalKnowledgeWorkResponse
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

    @field_validator("speaker", "text", mode="before")
    @classmethod
    def normalize_text(cls, value):
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value)
        return value


class ScriptPanel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shot: str = ""
    camera: str = ""
    location: str = ""
    visual_prompt: str = ""
    characters: list[str] = Field(default_factory=list)
    dialogue: list[ScriptDialogue] = Field(default_factory=list)
    sfx: list[str] = Field(default_factory=list)

    @field_validator("shot", "camera", "location", "visual_prompt", mode="before")
    @classmethod
    def normalize_text(cls, value):
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value)
        return value

    @field_validator("characters", "sfx", mode="before")
    @classmethod
    def normalize_str_list(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            return [text] if text else []
        return value


class ScriptPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(ge=1)
    layout: str = ""
    panels: list[ScriptPanel] = Field(min_length=1, max_length=9)

    @field_validator("layout", mode="before")
    @classmethod
    def normalize_layout(cls, value):
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value)
        return value


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
    knowledge_work_id: str = Field(default="", max_length=120)
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
