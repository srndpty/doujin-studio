"""プロジェクト編集・画像アップロード・描画・出力のHTTPルーター。"""

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException, Request, Response, status

from .. import layout_engine
from .. import preflight as preflight_module
from ..assets import path_to_asset_id, safe_component, stable_asset_name
from ..deletion import write_deletion_fence
from ..generator import generate_four_page_name
from ..mutation import ProjectRevisionConflictError, RenderCommitConflictError, mark_page_dirty
from ..rendering import (
    RenderInputChangedError,
    asset_to_id,
    build_production_status,
    invalidate_changed_pages,
    structure_signature,
)
from ..schemas import (
    CharacterReferenceResult,
    EmptyMutationResult,
    ExportResult,
    GenerateNameRequest,
    LayoutSuggestRequest,
    LayoutSuggestResult,
    MangaProject,
    OpenExportFolderResponse,
    PageRenderResult,
    PanelControlReference,
    PanelPageRenderResult,
    PreflightResponse,
    ProjectCreate,
    ProjectDeletionResponse,
    ProjectDetail,
    ProjectMutationResponse,
    ProjectProductionStatus,
    ProjectSummary,
    ReferenceAssetResult,
)
from .common import (
    PROJECT_MUTATION_ERROR_RESPONSES,
    _to_preflight_response,
    find_panel,
    find_panel_page_number,
    load_project_record,
    open_in_file_manager,
    parse_manga_json,
    save_content_addressed_request_image,
    to_detail,
    to_project_mutation_response,
    to_summary,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/projects", response_model=ProjectMutationResponse[EmptyMutationResult])
def create_project(
    payload: ProjectCreate, request: Request
) -> ProjectMutationResponse[EmptyMutationResult]:
    record = request.app.state.mutation.create(
        title=payload.title, work_name=payload.work_name, target_pages=payload.target_pages
    )
    project = to_detail(record, request.app.state.settings.export_dir)
    return ProjectMutationResponse(
        project=project,
        latest_revision=project.revision,
        result=EmptyMutationResult(),
    )


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


DELETION_TOMBSTONE_GLOB = "*.deleting-*"
DELETION_MARKER_GLOB = "*.deletion-pending"

# 成果物削除の結果。removed=完全削除、pending=tombstone/markerで自動再回収予定、
# orphaned=残骸も再回収手段も用意できず手動対応が必要。
RemovalOutcome = Literal["removed", "pending", "orphaned"]


def _write_deletion_marker(export_dir: Path, residual: Path) -> bool:
    """残骸ディレクトリ名をmarkerへ記録する。書込み成否を返す。

    任意パスの再帰削除を避けるため、保存するのはexport直下の単一ディレクトリ名のみ。
    起動時sweepは ``export_dir / name`` を再構成し、範囲外なら削除しない。
    """
    marker = export_dir / f"{residual.name}.deletion-pending"
    try:
        marker.write_text(residual.name, encoding="utf-8")
        return True
    except OSError:
        return False


def _unlink_marker(marker: Path) -> None:
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        # locked等で消せない場合はmarkerを残し、次回起動で再試行する。
        logger.warning("削除markerを片付けられませんでした: %s", marker)


def _remove_project_dir(project_dir: Path) -> RemovalOutcome:
    """成果物ディレクトリを削除する。tombstoneへos.replaceしてからrmtreeする。

    rename成功後の残骸はtombstone名でglob回収できる。renameごと失敗して元ディレクトリが
    残った場合はtombstone名にならないため、markerへ名前を記録して起動時に再試行する。
    marker書込みも失敗したら自動再回収できない孤児としてorphanedを返す。
    """
    if not project_dir.exists():
        return "removed"
    tombstone = project_dir.with_name(f"{project_dir.name}.deleting-{uuid.uuid4().hex}")
    renamed = False
    try:
        os.replace(project_dir, tombstone)
        renamed = True
        target = tombstone
    except OSError:
        # 退避に失敗（開かれたファイル等）してもそのまま削除を試みる。
        target = project_dir
    shutil.rmtree(target, ignore_errors=True)
    if not target.exists():
        return "removed"
    if renamed:
        # tombstone名で残存。起動時globで再回収できる。
        return "pending"
    if _write_deletion_marker(project_dir.parent, target):
        return "pending"
    logger.error("成果物を削除できず、再回収markerも作成できませんでした: %s", target)
    return "orphaned"


def sweep_deletion_tombstones(export_dir: Path) -> None:
    """削除に失敗して残った成果物を再回収する（起動時に呼ぶ）。

    markerに記録された残骸（rename失敗の元ディレクトリ含む）と、marker無しで残った
    tombstone（プロセスクラッシュ時の保険）の双方を掃除する。markerはexport直下の
    単一ディレクトリ名のみを指す前提で検証し、範囲外を指すmarkerは削除しない。
    """
    if not export_dir.exists():
        return
    root = export_dir.resolve()
    for marker in export_dir.glob(DELETION_MARKER_GLOB):
        try:
            name = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not name or name in {".", ".."} or name != Path(name).name:
            logger.warning("不正な削除markerを無視します: %s", marker)
            continue
        residual = root / name
        if residual.resolve().parent != root:
            logger.warning("範囲外の削除markerを無視します: %s", marker)
            continue
        if residual.exists():
            shutil.rmtree(residual, ignore_errors=True)
        if not residual.exists():
            _unlink_marker(marker)
    for tombstone in export_dir.glob(DELETION_TOMBSTONE_GLOB):
        shutil.rmtree(tombstone, ignore_errors=True)


@router.delete(
    "/api/projects/{project_id}",
    response_model=ProjectDeletionResponse,
    responses={202: {"model": ProjectDeletionResponse}},
)
async def delete_project(
    project_id: str, request: Request, response: Response
) -> ProjectDeletionResponse:
    """DBレコードを先に削除して新規ジョブ登録とworkerのpublishを止め、その後に
    進行中ジョブを停止・待機してから成果物を別スレッドで削除する。

    DBレコードを削除した時点で、enqueueはProjectNotFoundError、実行中workerも最新
    読み直しでproject不在となり、成果物の再作成やDB更新ができなくなる。これにより
    「削除後にworkerが復活させる」「commit失敗で成果物だけ失う」競合を避ける。
    """
    record = load_project_record(request, project_id)
    export_dir = request.app.state.settings.export_dir.resolve()
    project_dir = (export_dir / project_id).resolve()
    if project_dir.parent != export_dir:
        raise HTTPException(status_code=400, detail="プロジェクト保存先が不正です")

    # 1) 先にDBレコードを削除・commitして、新規生成の入口を閉じる。
    with request.app.state.SessionLocal() as session:
        current = request.app.state.repository.get(session, record.id)
        if current is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        request.app.state.repository.delete(session, current)
        session.commit()

    # DB削除後すぐに通常名directory用の永続fenceを作る。cancel不能workerは生成前後に
    # fenceを確認し、遅延publishした成果物を回収する。
    fence_written = write_deletion_fence(export_dir, project_id)

    # 2) 入口を閉じた後に進行中ジョブを掃き出す。個別cancelが失敗しても残りを止め、
    #    finallyで成果物掃除へ必ず進む。
    manager = request.app.state.job_manager
    active_jobs = [
        job
        for job in list(manager.jobs.values())
        if job.project_id == project_id and job.status not in {"done", "error", "cancelled"}
    ]
    cancel_failed = False
    outcome: RemovalOutcome = "orphaned"
    try:
        for job in active_jobs:
            try:
                await request.app.state.generation.cancel(job)
            except Exception:
                cancel_failed = True
                logger.exception(
                    "プロジェクト削除時に生成ジョブを停止できませんでした: project=%s job=%s",
                    project_id,
                    job.id,
                )
    finally:
        # 3) 同期rmtreeはイベントループを止めるため別スレッドで削除する。
        try:
            outcome = await asyncio.to_thread(_remove_project_dir, project_dir)
        except Exception:
            # 想定外のfilesystem例外でもDB削除済みの事実を返し、手動対応先を通知する。
            logger.exception("プロジェクト成果物の削除処理に失敗しました: %s", project_dir)
            outcome = "orphaned"

    cleanup_state: Literal["complete", "pending", "manual_required"]
    if outcome == "removed":
        cleanup_state = "complete"
    elif outcome == "pending":
        cleanup_state = "pending"
    else:
        cleanup_state = "manual_required"
    if cleanup_state != "complete" or cancel_failed:
        response.status_code = status.HTTP_202_ACCEPTED
    if cancel_failed and not fence_written:
        cleanup_state = "manual_required"
        response.status_code = status.HTTP_202_ACCEPTED
    return ProjectDeletionResponse(
        deleted=True,
        cleanup_state=cleanup_state,
        manual_cleanup_path=str(project_dir) if cleanup_state == "manual_required" else None,
        generation_stop_failed=cancel_failed,
    )


@router.post(
    "/api/projects/{project_id}/generate-name",
    response_model=ProjectMutationResponse[EmptyMutationResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def generate_name(
    project_id: str, payload: GenerateNameRequest, request: Request, revision: int
) -> ProjectMutationResponse[EmptyMutationResult]:
    checked = request.app.state.mutation.require_revision(project_id, revision)
    manga = generate_four_page_name(
        title=checked.manga.title,
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
        expected_revision=revision,
        increment_epoch=True,
    )
    await request.app.state.generation.cancel_before_epoch(
        project_id, mutation_result.project.generation_epoch
    )
    return to_project_mutation_response(
        request, project_id, EmptyMutationResult(), snapshot=mutation_result.project
    )


@router.put(
    "/api/projects/{project_id}/manga-json",
    response_model=ProjectMutationResponse[EmptyMutationResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def update_manga_json(
    project_id: str,
    payload: MangaProject,
    request: Request,
    revision: int,
) -> ProjectMutationResponse[EmptyMutationResult]:
    previous = request.app.state.mutation.require_revision(project_id, revision).manga
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
    return to_project_mutation_response(
        request, project_id, EmptyMutationResult(), snapshot=mutation_result.project
    )


@router.post(
    "/api/projects/{project_id}/characters/{character_id:path}/reference-image",
    response_model=ProjectMutationResponse[CharacterReferenceResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def upload_character_reference(
    project_id: str,
    character_id: str,
    request: Request,
    revision: int,
) -> ProjectMutationResponse[CharacterReferenceResult]:
    manga0 = request.app.state.mutation.require_revision(project_id, revision).manga
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
    return to_project_mutation_response(
        request,
        project_id,
        CharacterReferenceResult(character_id=character_id, asset=asset_id),
        snapshot=mutation_result.project,
    )


@router.post(
    "/api/projects/{project_id}/locations/{location_id:path}/reference-image",
    response_model=ProjectMutationResponse[ReferenceAssetResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def upload_location_reference(
    project_id: str,
    location_id: str,
    request: Request,
    revision: int,
) -> ProjectMutationResponse[ReferenceAssetResult]:
    manga0 = request.app.state.mutation.require_revision(project_id, revision).manga
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
    return to_project_mutation_response(
        request,
        project_id,
        ReferenceAssetResult(target_id=location_id, asset=asset_id),
        snapshot=mutation_result.project,
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id:path}/controls/{kind}/reference-image",
    response_model=ProjectMutationResponse[ReferenceAssetResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def upload_panel_control_reference(
    project_id: str,
    panel_id: str,
    kind: str,
    request: Request,
    load_node_id: str,
    revision: int,
) -> ProjectMutationResponse[ReferenceAssetResult]:
    if kind not in {"pose", "depth", "lineart", "background"}:
        raise HTTPException(status_code=422, detail="Control参照種別が不正です")
    checked = request.app.state.mutation.require_revision(project_id, revision)
    find_panel(checked.manga, panel_id)
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
    return to_project_mutation_response(
        request,
        project_id,
        ReferenceAssetResult(target_id=target_id, asset=asset_id),
        snapshot=mutation_result.project,
    )


@router.post(
    "/api/projects/{project_id}/pages/{page_number}/overlays/{overlay_id:path}/{asset_kind}",
    response_model=ProjectMutationResponse[ReferenceAssetResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def upload_overlay_asset(
    project_id: str,
    page_number: int,
    overlay_id: str,
    asset_kind: str,
    request: Request,
    revision: int,
) -> ProjectMutationResponse[ReferenceAssetResult]:
    if asset_kind not in {"asset", "mask"}:
        raise HTTPException(status_code=422, detail="overlayアセット種別が不正です")
    manga0 = request.app.state.mutation.require_revision(project_id, revision).manga
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
    return to_project_mutation_response(
        request,
        project_id,
        ReferenceAssetResult(target_id=overlay_id, asset=asset_id),
        snapshot=mutation_result.project,
    )


@router.post(
    "/api/projects/{project_id}/pages/{page_number}/layout/suggest",
    response_model=ProjectMutationResponse[LayoutSuggestResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
def suggest_page_layout(
    project_id: str,
    page_number: int,
    request: Request,
    revision: int,
    payload: Annotated[LayoutSuggestRequest | None, Body()] = None,
) -> ProjectMutationResponse[LayoutSuggestResult]:
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

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=relayout
    )
    layout_family = mutation_result.result
    return to_project_mutation_response(
        request,
        project_id,
        LayoutSuggestResult(page=page_number, layout_family=layout_family),
        snapshot=mutation_result.project,
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
    response_model=ProjectMutationResponse[PageRenderResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
def render_page_endpoint(
    project_id: str, page_number: int, request: Request, revision: int
) -> ProjectMutationResponse[PageRenderResult]:
    settings = request.app.state.settings
    record = load_project_record(request, project_id)
    if record.revision != revision:
        raise ProjectRevisionConflictError(project_id, revision)
    snapshot = parse_manga_json(record.manga_json)
    if not any(page.page == page_number for page in snapshot.pages):
        raise HTTPException(status_code=404, detail="ページが見つかりません")
    try:
        rendered = request.app.state.rendering.render_and_commit_page(
            project_id, snapshot, record.revision, page_number
        )
    except (RenderCommitConflictError, RenderInputChangedError) as exc:
        raise ProjectRevisionConflictError(project_id, revision) from exc
    manga = rendered.project.manga
    page = next(item for item in manga.pages if item.page == page_number)
    issues = preflight_module.preflight_page(manga, page, export_dir=settings.export_dir)
    return to_project_mutation_response(
        request,
        project_id,
        PageRenderResult(
            page=page_number,
            page_asset=asset_to_id(rendered.asset, settings.export_dir),
            warnings=rendered.warnings,
            preflight=_to_preflight_response(project_id, page_number, issues),
        ),
        snapshot=rendered.project,
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/render-page",
    response_model=ProjectMutationResponse[PanelPageRenderResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
def render_panel_page(
    project_id: str, panel_id: str, request: Request, revision: int
) -> ProjectMutationResponse[PanelPageRenderResult]:
    settings = request.app.state.settings
    record = load_project_record(request, project_id)
    if record.revision != revision:
        raise ProjectRevisionConflictError(project_id, revision)
    snapshot = parse_manga_json(record.manga_json)
    page_number = find_panel_page_number(snapshot, panel_id)
    try:
        rendered = request.app.state.rendering.render_and_commit_page(
            project_id, snapshot, record.revision, page_number
        )
    except (RenderCommitConflictError, RenderInputChangedError) as exc:
        raise ProjectRevisionConflictError(project_id, revision) from exc
    return to_project_mutation_response(
        request,
        project_id,
        PanelPageRenderResult(
            panel_id=panel_id,
            page_asset=asset_to_id(rendered.asset, settings.export_dir),
            warnings=rendered.warnings,
        ),
        snapshot=rendered.project,
    )


@router.post(
    "/api/projects/{project_id}/export/cbz",
    response_model=ProjectMutationResponse[ExportResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
def export_project_cbz(
    project_id: str, request: Request, revision: int
) -> ProjectMutationResponse[ExportResult]:
    settings = request.app.state.settings
    try:
        result = request.app.state.project_render.export_cbz(project_id, expected_revision=revision)
    except (RenderCommitConflictError, RenderInputChangedError) as exc:
        raise ProjectRevisionConflictError(project_id, revision) from exc
    return to_project_mutation_response(
        request,
        project_id,
        ExportResult(
            cbz_asset=asset_to_id(result.cbz_path, settings.export_dir),
            absolute_path=str(result.cbz_path.resolve()),
            warnings=result.warnings,
        ),
        snapshot=result.project,
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
