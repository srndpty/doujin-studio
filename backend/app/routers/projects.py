"""プロジェクト編集・画像アップロード・描画・出力のHTTPルーター。"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request

from .. import layout_engine
from .. import preflight as preflight_module
from ..assets import path_to_asset_id, safe_component, stable_asset_name
from ..generator import generate_four_page_name
from ..mutation import mark_page_dirty
from ..rendering import (
    asset_to_id,
    build_production_status,
    invalidate_changed_pages,
    structure_signature,
)
from ..schemas import (
    CharacterReferenceResponse,
    ExportResponse,
    GenerateNameRequest,
    LayoutSuggestRequest,
    LayoutSuggestResponse,
    MangaProject,
    OpenExportFolderResponse,
    PageRenderResponse,
    PanelControlReference,
    PanelPageRenderResponse,
    PreflightResponse,
    ProjectCreate,
    ProjectDetail,
    ProjectProductionStatus,
    ProjectSummary,
    ReferenceAssetResponse,
)
from .common import (
    _to_preflight_response,
    find_panel,
    find_panel_page_number,
    load_project_record,
    open_in_file_manager,
    parse_manga_json,
    save_content_addressed_request_image,
    to_detail,
    to_summary,
)

router = APIRouter()


@router.post("/api/projects", response_model=ProjectDetail)
def create_project(payload: ProjectCreate, request: Request) -> ProjectDetail:
    record = request.app.state.mutation.create(
        title=payload.title, work_name=payload.work_name, target_pages=payload.target_pages
    )
    return to_detail(record, request.app.state.settings.export_dir)


@router.get("/api/projects", response_model=list[ProjectSummary])
def list_projects(request: Request) -> list[ProjectSummary]:
    with request.app.state.SessionLocal() as session:
        records = request.app.state.repository.list_ordered(session)
    return [to_summary(record) for record in records]


@router.get("/api/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, request: Request) -> ProjectDetail:
    return to_detail(
        load_project_record(request, project_id), request.app.state.settings.export_dir
    )


@router.post("/api/projects/{project_id}/generate-name", response_model=ProjectDetail)
async def generate_name(
    project_id: str, payload: GenerateNameRequest, request: Request
) -> ProjectDetail:
    record = load_project_record(request, project_id)
    manga = generate_four_page_name(
        title=record.title,
        work_name=payload.work_name,
        character_a=payload.character_a,
        character_b=payload.character_b,
        situation=payload.situation,
        ending_direction=payload.ending_direction,
        target_pages=payload.target_pages,
    )
    mutation_result = request.app.state.mutation.replace(
        project_id,
        manga,
        expected_revision=payload.revision,
        increment_epoch=True,
    )
    await request.app.state.generation.cancel_before_epoch(
        project_id, mutation_result.project.generation_epoch
    )
    return to_detail(load_project_record(request, project_id))


@router.put("/api/projects/{project_id}/manga-json", response_model=ProjectDetail)
async def update_manga_json(
    project_id: str,
    payload: MangaProject,
    request: Request,
    revision: int,
) -> ProjectDetail:
    previous = parse_manga_json(load_project_record(request, project_id).manga_json)
    structure_changed = structure_signature(payload) != structure_signature(previous)
    invalidate_changed_pages(payload, previous)
    mutation_result = request.app.state.mutation.replace(
        project_id,
        payload,
        expected_revision=revision,
        increment_epoch=structure_changed,
    )
    if structure_changed:
        await request.app.state.generation.cancel_before_epoch(
            project_id, mutation_result.project.generation_epoch
        )
    return to_detail(
        load_project_record(request, project_id), request.app.state.settings.export_dir
    )


@router.post(
    "/api/projects/{project_id}/characters/{character_id:path}/reference-image",
    response_model=CharacterReferenceResponse,
)
async def upload_character_reference(
    project_id: str,
    character_id: str,
    request: Request,
    revision: int,
) -> CharacterReferenceResponse:
    manga0 = parse_manga_json(load_project_record(request, project_id).manga_json)
    if not any(item.id == character_id for item in manga0.characters):
        raise HTTPException(status_code=404, detail="キャラクターが見つかりません")
    asset_dir = (
        request.app.state.settings.export_dir
        / safe_component(project_id, "project")
        / "references"
        / stable_asset_name(character_id, "character").removesuffix(".png")
    )
    target, _created = await save_content_addressed_request_image(request, asset_dir, "character")
    asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)

    def mutate(manga: MangaProject) -> None:
        character = next((item for item in manga.characters if item.id == character_id), None)
        if character is None:
            raise HTTPException(status_code=404, detail="キャラクターが見つかりません")
        character.reference_image_asset = asset_id

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=mutate
    )
    manga = mutation_result.project.manga
    revision_out = mutation_result.project.revision
    return CharacterReferenceResponse(
        character_id=character_id,
        asset=asset_id,
        manga_json=manga,
        revision=revision_out,
    )


@router.post(
    "/api/projects/{project_id}/locations/{location_id:path}/reference-image",
    response_model=ReferenceAssetResponse,
)
async def upload_location_reference(
    project_id: str,
    location_id: str,
    request: Request,
    revision: int,
) -> ReferenceAssetResponse:
    manga0 = parse_manga_json(load_project_record(request, project_id).manga_json)
    if not any(item.id == location_id for item in manga0.locations):
        raise HTTPException(status_code=404, detail="ロケーションが見つかりません")
    asset_dir = (
        request.app.state.settings.export_dir
        / safe_component(project_id, "project")
        / "locations"
        / stable_asset_name(location_id, "location").removesuffix(".png")
    )
    target, _created = await save_content_addressed_request_image(request, asset_dir, "location")
    asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)

    def mutate(manga: MangaProject) -> None:
        location = next((item for item in manga.locations if item.id == location_id), None)
        if location is None:
            raise HTTPException(status_code=404, detail="ロケーションが見つかりません")
        location.reference_image_asset = asset_id

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=mutate
    )
    manga = mutation_result.project.manga
    revision_out = mutation_result.project.revision
    return ReferenceAssetResponse(
        target_id=location_id, asset=asset_id, manga_json=manga, revision=revision_out
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id:path}/controls/{kind}/reference-image",
    response_model=ReferenceAssetResponse,
)
async def upload_panel_control_reference(
    project_id: str,
    panel_id: str,
    kind: str,
    request: Request,
    load_node_id: str,
    revision: int,
) -> ReferenceAssetResponse:
    if kind not in {"pose", "depth", "lineart", "background"}:
        raise HTTPException(status_code=422, detail="Control参照種別が不正です")
    find_panel(parse_manga_json(load_project_record(request, project_id).manga_json), panel_id)
    asset_dir = (
        request.app.state.settings.export_dir
        / safe_component(project_id, "project")
        / "controls"
        / stable_asset_name(panel_id, "panel").removesuffix(".png")
    )
    target, _created = await save_content_addressed_request_image(request, asset_dir, kind)
    asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)
    new_control_id = str(uuid.uuid4())

    def mutate(manga: MangaProject) -> str:
        panel = find_panel(manga, panel_id)
        existing = next((item for item in panel.control_references if item.kind == kind), None)
        if existing:
            existing.asset = asset_id
            existing.load_node_id = load_node_id
            return existing.id
        control = PanelControlReference.model_validate(
            {"id": new_control_id, "kind": kind, "asset": asset_id, "load_node_id": load_node_id}
        )
        panel.control_references.append(control)
        return control.id

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=mutate
    )
    target_id = mutation_result.result
    manga = mutation_result.project.manga
    revision_out = mutation_result.project.revision
    return ReferenceAssetResponse(
        target_id=target_id, asset=asset_id, manga_json=manga, revision=revision_out
    )


@router.post(
    "/api/projects/{project_id}/pages/{page_number}/overlays/{overlay_id:path}/{asset_kind}",
    response_model=ReferenceAssetResponse,
)
async def upload_overlay_asset(
    project_id: str,
    page_number: int,
    overlay_id: str,
    asset_kind: str,
    request: Request,
    revision: int,
) -> ReferenceAssetResponse:
    if asset_kind not in {"asset", "mask"}:
        raise HTTPException(status_code=422, detail="overlayアセット種別が不正です")
    manga0 = parse_manga_json(load_project_record(request, project_id).manga_json)
    page0 = next((item for item in manga0.pages if item.page == page_number), None)
    if page0 is None:
        raise HTTPException(status_code=404, detail="ページが見つかりません")
    if not any(item.id == overlay_id for item in page0.overlay_elements):
        raise HTTPException(status_code=404, detail="overlayが見つかりません")
    asset_dir = (
        request.app.state.settings.export_dir
        / safe_component(project_id, "project")
        / "overlays"
        / stable_asset_name(overlay_id, "overlay").removesuffix(".png")
    )
    target, _created = await save_content_addressed_request_image(
        request,
        asset_dir,
        asset_kind,
        preserve_alpha=asset_kind == "asset",
    )
    asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)

    def mutate(manga: MangaProject) -> None:
        page = next((item for item in manga.pages if item.page == page_number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="ページが見つかりません")
        overlay = next((item for item in page.overlay_elements if item.id == overlay_id), None)
        if overlay is None:
            raise HTTPException(status_code=404, detail="overlayが見つかりません")
        if asset_kind == "mask":
            overlay.mask_asset = asset_id
        else:
            overlay.asset = asset_id
        mark_page_dirty(page)

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=mutate
    )
    manga = mutation_result.project.manga
    revision_out = mutation_result.project.revision
    return ReferenceAssetResponse(
        target_id=overlay_id, asset=asset_id, manga_json=manga, revision=revision_out
    )


@router.post(
    "/api/projects/{project_id}/pages/{page_number}/layout/suggest",
    response_model=LayoutSuggestResponse,
)
def suggest_page_layout(
    project_id: str,
    page_number: int,
    request: Request,
    payload: Annotated[LayoutSuggestRequest | None, Body()] = None,
) -> LayoutSuggestResponse:
    def relayout(manga: MangaProject):
        page = next((item for item in manga.pages if item.page == page_number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="ページが見つかりません")
        page_index = manga.pages.index(page)
        previous_family = manga.pages[page_index - 1].layout_family if page_index > 0 else None
        layout_engine.relayout_page(
            page,
            manga.page_layout,
            rtl=manga.reading_direction == "rtl",
            family=payload.family if payload else None,
            previous_family=previous_family,
            page_index=page_index,
            total_pages=len(manga.pages),
        )
        mark_page_dirty(page)
        return page.layout_family

    mutation_result = request.app.state.mutation.mutate_local(project_id, relayout)
    layout_family = mutation_result.result
    manga = mutation_result.project.manga
    revision = mutation_result.project.revision
    return LayoutSuggestResponse(
        project_id=project_id,
        page=page_number,
        layout_family=layout_family,
        manga_json=manga,
        revision=revision,
    )


@router.post(
    "/api/projects/{project_id}/pages/{page_number}/preflight",
    response_model=PreflightResponse,
)
def preflight_page_endpoint(
    project_id: str,
    page_number: int,
    request: Request,
    payload: Annotated[MangaProject | None, Body()] = None,
) -> PreflightResponse:
    manga = payload or parse_manga_json(load_project_record(request, project_id).manga_json)
    page = next((item for item in manga.pages if item.page == page_number), None)
    if page is None:
        raise HTTPException(status_code=404, detail="ページが見つかりません")
    issues = preflight_module.preflight_page(
        manga, page, export_dir=request.app.state.settings.export_dir
    )
    return _to_preflight_response(project_id, page_number, issues)


@router.post("/api/projects/{project_id}/preflight", response_model=PreflightResponse)
def preflight_project_endpoint(project_id: str, request: Request) -> PreflightResponse:
    manga = parse_manga_json(load_project_record(request, project_id).manga_json)
    issues = preflight_module.preflight_project(manga, request.app.state.settings.export_dir)
    return _to_preflight_response(project_id, None, issues)


@router.post(
    "/api/projects/{project_id}/pages/{page_number}/render",
    response_model=PageRenderResponse,
)
def render_page_endpoint(project_id: str, page_number: int, request: Request) -> PageRenderResponse:
    settings = request.app.state.settings
    record = load_project_record(request, project_id)
    snapshot = parse_manga_json(record.manga_json)
    if not any(page.page == page_number for page in snapshot.pages):
        raise HTTPException(status_code=404, detail="ページが見つかりません")
    rendered = request.app.state.rendering.render_and_commit_page(
        project_id, snapshot, record.revision, page_number
    )
    manga = rendered.project.manga
    page = next(item for item in manga.pages if item.page == page_number)
    issues = preflight_module.preflight_page(manga, page, export_dir=settings.export_dir)
    return PageRenderResponse(
        project_id=project_id,
        page=page_number,
        page_asset=asset_to_id(rendered.asset, settings.export_dir),
        manga_json=manga,
        revision=rendered.project.revision,
        warnings=rendered.warnings,
        preflight=_to_preflight_response(project_id, page_number, issues),
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/render-page",
    response_model=PanelPageRenderResponse,
)
def render_panel_page(project_id: str, panel_id: str, request: Request) -> PanelPageRenderResponse:
    settings = request.app.state.settings
    record = load_project_record(request, project_id)
    snapshot = parse_manga_json(record.manga_json)
    page_number = find_panel_page_number(snapshot, panel_id)
    rendered = request.app.state.rendering.render_and_commit_page(
        project_id, snapshot, record.revision, page_number
    )
    return PanelPageRenderResponse(
        project_id=project_id,
        panel_id=panel_id,
        page_asset=asset_to_id(rendered.asset, settings.export_dir),
        manga_json=rendered.project.manga,
        revision=rendered.project.revision,
        warnings=rendered.warnings,
    )


@router.post("/api/projects/{project_id}/export/cbz", response_model=ExportResponse)
def export_project_cbz(project_id: str, request: Request) -> ExportResponse:
    settings = request.app.state.settings
    result = request.app.state.project_render.export_cbz(project_id)
    return ExportResponse(
        project_id=project_id,
        cbz_asset=asset_to_id(result.cbz_path, settings.export_dir),
        absolute_path=str(result.cbz_path.resolve()),
        revision=result.project.revision,
        manga_json=result.project.manga,
        warnings=result.warnings,
    )


@router.post(
    "/api/projects/{project_id}/export/open-folder",
    response_model=OpenExportFolderResponse,
)
def open_project_export_folder(project_id: str, request: Request) -> OpenExportFolderResponse:
    load_project_record(request, project_id)
    export_dir = request.app.state.settings.export_dir.resolve()
    project_dir = (export_dir / project_id).resolve()
    if project_dir.parent != export_dir:
        raise HTTPException(status_code=400, detail="出力フォルダが不正です")
    project_dir.mkdir(parents=True, exist_ok=True)
    cbz_files = list(project_dir.glob("*.cbz"))
    cbz_path = max(cbz_files, key=lambda path: path.stat().st_mtime) if cbz_files else project_dir
    open_in_file_manager(cbz_path)
    return OpenExportFolderResponse(
        project_id=project_id,
        folder_path=str(project_dir),
        cbz_path=str(cbz_path),
        cbz_exists=cbz_path.is_file(),
    )


@router.get("/api/projects/{project_id}/production-status", response_model=ProjectProductionStatus)
def get_production_status(project_id: str, request: Request) -> ProjectProductionStatus:
    manga = parse_manga_json(load_project_record(request, project_id).manga_json)
    return build_production_status(project_id, manga, request.app.state.settings.export_dir)
