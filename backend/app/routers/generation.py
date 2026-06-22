"""画像生成ジョブ・候補選択・同期生成のHTTPルーター。"""

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, WebSocket, WebSocketDisconnect

from ..generation_service import (
    apply_candidate_selection,
    find_panel_and_page,
    register_generation_candidate,
)
from ..image_backends import StubImageBackend
from ..jobs import TERMINAL_JOB_STATUSES, JobManager
from ..mutation import mark_page_dirty
from ..prompt_composer import compose_panel_prompts
from ..rendering import asset_to_id, find_inconsistent_selected_panel, render_snapshot_pages
from ..schemas import (
    BatchGenerationJobCreate,
    BatchGenerationJobResponse,
    GenerationJobCreate,
    GenerationJobResponse,
    MangaProject,
    PanelImageGenerationResponse,
    PanelPageRenderResponse,
    PromptPreviewResponse,
    RenderRequest,
    RenderResponse,
)
from .common import (
    GENERATION_ERROR_RESPONSES,
    ensure_generation_succeeded,
    find_panel,
    find_panel_page_number,
    get_job_or_404,
    load_project_record,
    parse_manga_json,
    to_job_response,
)

router = APIRouter()


@router.post(
    "/api/projects/{project_id}/render",
    response_model=RenderResponse,
    responses=GENERATION_ERROR_RESPONSES,
)
async def render_project(
    project_id: str,
    request: Request,
    payload: Annotated[RenderRequest | None, Body()] = None,
) -> RenderResponse:
    record = load_project_record(request, project_id)
    manga = parse_manga_json(record.manga_json)
    settings = request.app.state.settings
    force = payload.force if payload else False
    started_epoch = record.generation_epoch

    def ensure_same_epoch() -> None:
        if request.app.state.mutation.current_epoch(project_id) != started_epoch:
            raise HTTPException(
                status_code=409,
                detail="レンダリング中に作品構成が更新されました。最新で再実行してください。",
            )

    for page in manga.pages:
        for panel in page.panels:
            if not (force or not panel.image_asset):
                continue
            ensure_same_epoch()
            job = request.app.state.generation.find_active_panel_job(project_id, panel.panel_id)
            if job is None:
                job = request.app.state.generation.start(
                    project_id,
                    [panel.panel_id],
                    1,
                    "全体生成ジョブを登録しました",
                    expected_epoch=started_epoch,
                )[0]
            ensure_generation_succeeded(await request.app.state.generation.await_completion(job))
            ensure_same_epoch()

    ensure_same_epoch()
    latest = load_project_record(request, project_id)
    snapshot = parse_manga_json(latest.manga_json)
    inconsistent = find_inconsistent_selected_panel(snapshot, settings.export_dir)
    if inconsistent is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{inconsistent.panel_id}: 採用画像が欠損/不整合です。"
                "再選択または再生成してから出力してください。"
            ),
        )
    published_by_request: dict[Path, bool] = {}
    try:
        page_assets, warnings = render_snapshot_pages(
            project_id,
            snapshot,
            settings.export_dir,
            latest.revision,
            ownership=published_by_request,
        )
        manga, revision = request.app.state.rendering.commit_rendered_pages(
            project_id, snapshot, page_assets, expected_epoch=started_epoch
        )
    except Exception:
        request.app.state.rendering.cleanup_published_assets(project_id, published_by_request)
        raise
    return RenderResponse(
        project_id=project_id,
        page_assets=[asset_to_id(path, settings.export_dir) for path in page_assets],
        manga_json=manga,
        revision=revision,
        warnings=warnings,
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/generate-image",
    response_model=PanelImageGenerationResponse,
    responses=GENERATION_ERROR_RESPONSES,
)
async def generate_panel_image(
    project_id: str,
    panel_id: str,
    request: Request,
    payload: Annotated[GenerationJobCreate | None, Body()] = None,
) -> PanelImageGenerationResponse:
    if request.app.state.generation.find_active_panel_job(project_id, panel_id):
        raise HTTPException(status_code=409, detail="このコマは画像生成中です")
    candidate_count = payload.candidate_count if payload else 1
    job = request.app.state.generation.start(
        project_id, [panel_id], candidate_count, "画像生成ジョブを登録しました"
    )[0]
    ensure_generation_succeeded(await request.app.state.generation.await_completion(job))
    latest = load_project_record(request, project_id)
    return PanelImageGenerationResponse(
        project_id=project_id,
        panel_id=panel_id,
        manga_json=parse_manga_json(latest.manga_json),
        revision=latest.revision,
    )


@router.get(
    "/api/projects/{project_id}/panels/{panel_id}/prompt-preview",
    response_model=PromptPreviewResponse,
)
def preview_panel_prompt(project_id: str, panel_id: str, request: Request) -> PromptPreviewResponse:
    manga = parse_manga_json(load_project_record(request, project_id).manga_json)
    panel = find_panel(manga, panel_id)
    positive, negative = compose_panel_prompts(manga, panel)
    return PromptPreviewResponse(
        panel_id=panel_id,
        positive_prompt=positive,
        negative_prompt=negative,
        character_ids=panel.characters,
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/generation-jobs",
    response_model=GenerationJobResponse,
)
async def create_generation_job(
    project_id: str,
    panel_id: str,
    request: Request,
    payload: Annotated[GenerationJobCreate | None, Body()] = None,
) -> GenerationJobResponse:
    if request.app.state.generation.find_active_panel_job(project_id, panel_id):
        raise HTTPException(status_code=409, detail="このコマは画像生成中です")
    job = request.app.state.generation.start(
        project_id,
        [panel_id],
        payload.candidate_count if payload else 1,
        "画像生成ジョブを登録しました",
    )[0]
    return to_job_response(job)


@router.post(
    "/api/projects/{project_id}/generation-jobs", response_model=BatchGenerationJobResponse
)
async def create_batch_generation_jobs(
    project_id: str,
    request: Request,
    payload: Annotated[BatchGenerationJobCreate | None, Body()] = None,
) -> BatchGenerationJobResponse:
    manga = parse_manga_json(load_project_record(request, project_id).manga_json)
    options = payload or BatchGenerationJobCreate()
    panels = [
        panel
        for page in manga.pages
        if options.page is None or page.page == options.page
        for panel in page.panels
    ]
    if not panels:
        raise HTTPException(status_code=404, detail="生成対象のコマが見つかりません")
    target_ids = [panel.panel_id for panel in panels]
    jobs = request.app.state.generation.start(
        project_id,
        target_ids,
        options.candidate_count,
        "一括生成キューへ登録しました",
        skip_active=True,
    )
    return BatchGenerationJobResponse(jobs=[to_job_response(job) for job in jobs])


@router.get("/api/generation-jobs/{job_id}", response_model=GenerationJobResponse)
def get_generation_job(job_id: str, request: Request) -> GenerationJobResponse:
    return to_job_response(get_job_or_404(request, job_id))


@router.get(
    "/api/projects/{project_id}/generation-jobs", response_model=list[GenerationJobResponse]
)
def list_generation_jobs(project_id: str, request: Request) -> list[GenerationJobResponse]:
    load_project_record(request, project_id)
    manager: JobManager = request.app.state.job_manager
    return [to_job_response(job) for job in manager.list_for_project(project_id)]


@router.post("/api/generation-jobs/{job_id}/cancel", response_model=GenerationJobResponse)
async def cancel_generation_job(job_id: str, request: Request) -> GenerationJobResponse:
    job = get_job_or_404(request, job_id)
    return to_job_response(await request.app.state.generation.cancel(job))


@router.websocket("/api/generation-jobs/{job_id}/ws")
async def generation_job_websocket(websocket: WebSocket, job_id: str) -> None:
    manager: JobManager = websocket.app.state.job_manager
    job = manager.get(job_id)
    if job is None:
        await websocket.close(code=4404, reason="生成ジョブが見つかりません")
        return
    await websocket.accept()
    try:
        while True:
            revision = job.revision
            await websocket.send_json(job.as_dict())
            if job.status in TERMINAL_JOB_STATUSES:
                return
            job = await manager.wait_for_change(job_id, revision)
    except WebSocketDisconnect:
        return


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/candidates/{candidate_id}/select",
    response_model=PanelPageRenderResponse,
)
def select_panel_candidate(
    project_id: str,
    panel_id: str,
    candidate_id: str,
    request: Request,
) -> PanelPageRenderResponse:
    settings = request.app.state.settings

    def select(manga: MangaProject) -> int:
        panel = find_panel(manga, panel_id)
        candidate = next((item for item in panel.image_candidates if item.id == candidate_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail="画像候補が見つかりません")
        apply_candidate_selection(panel, candidate)
        page_number = find_panel_page_number(manga, panel_id)
        page = next(item for item in manga.pages if item.page == page_number)
        mark_page_dirty(page)
        return page_number

    mutation_result = request.app.state.mutation.mutate_local(project_id, select)
    page_number = mutation_result.result
    manga = mutation_result.project.manga
    revision = mutation_result.project.revision
    page_asset, warnings, manga, revision = request.app.state.rendering.render_and_commit_page(
        project_id, manga, revision, page_number
    )
    return PanelPageRenderResponse(
        project_id=project_id,
        panel_id=panel_id,
        page_asset=asset_to_id(page_asset, settings.export_dir),
        manga_json=manga,
        revision=revision,
        warnings=warnings,
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/use-stub",
    response_model=PanelImageGenerationResponse,
)
async def use_stub_panel_image(
    project_id: str,
    panel_id: str,
    request: Request,
) -> PanelImageGenerationResponse:
    panel = find_panel(
        parse_manga_json(load_project_record(request, project_id).manga_json), panel_id
    )
    settings = request.app.state.settings
    candidate_id = str(uuid.uuid4())
    target = settings.export_dir / project_id / "panels" / panel_id / f"{candidate_id}.png"
    result = await StubImageBackend().generate_panel(
        project_id, panel, settings.export_dir, target_path=target
    )

    def add(manga: MangaProject) -> None:
        target_panel, page = find_panel_and_page(manga, panel_id)
        if target_panel is None:
            raise HTTPException(status_code=404, detail="コマが見つかりません")
        register_generation_candidate(target_panel, target_panel, result, candidate_id)
        if page is not None:
            mark_page_dirty(page)

    mutation_result = request.app.state.mutation.mutate_local(project_id, add)
    manga = mutation_result.project.manga
    revision = mutation_result.project.revision
    return PanelImageGenerationResponse(
        project_id=project_id, panel_id=panel_id, manga_json=manga, revision=revision
    )
