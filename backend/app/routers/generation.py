"""画像生成ジョブ・候補選択・同期生成のHTTPルーター。"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, WebSocket, WebSocketDisconnect

from ..generation_service import (
    apply_candidate_selection,
    ensure_sync_generation_succeeded,
    find_panel_and_page,
    register_generation_candidate,
)
from ..generator import suggest_candidate_count
from ..image_backends import StubImageBackend
from ..jobs import TERMINAL_JOB_STATUSES, JobManager
from ..mutation import (
    ProjectMutationPartiallyAppliedError,
    ProjectRevisionConflictError,
    RenderCommitConflictError,
    mark_page_dirty,
)
from ..prompt_composer import compose_panel_prompts
from ..rendering import RenderInputChangedError, asset_to_id
from ..schemas import (
    BatchGenerationJobCreate,
    BatchGenerationJobResult,
    GenerationJobCreate,
    GenerationJobResponse,
    MangaProject,
    PanelImageGenerationResult,
    PanelPageRenderResult,
    ProjectMutationResponse,
    ProjectRenderResult,
    PromptPreviewResponse,
    RenderRequest,
)
from .common import (
    CANDIDATE_SELECT_ERROR_RESPONSES,
    GENERATION_ERROR_RESPONSES,
    PROJECT_MUTATION_ERROR_RESPONSES,
    find_panel,
    find_panel_page_number,
    get_job_or_404,
    load_project_record,
    parse_manga_json,
    to_job_response,
    to_project_mutation_response,
)

router = APIRouter()


@router.post(
    "/api/projects/{project_id}/render",
    response_model=ProjectMutationResponse[ProjectRenderResult],
    responses=GENERATION_ERROR_RESPONSES,
)
async def render_project(
    project_id: str,
    request: Request,
    revision: int,
    payload: Annotated[RenderRequest | None, Body()] = None,
) -> ProjectMutationResponse[ProjectRenderResult]:
    settings = request.app.state.settings
    force = payload.force if payload else False
    try:
        result = await request.app.state.project_render.render_project(
            project_id, force=force, expected_revision=revision
        )
    except (RenderCommitConflictError, RenderInputChangedError) as exc:
        raise ProjectRevisionConflictError(project_id, revision) from exc
    return to_project_mutation_response(
        request,
        project_id,
        ProjectRenderResult(
            page_assets=[asset_to_id(path, settings.export_dir) for path in result.page_assets],
            warnings=result.warnings,
        ),
        snapshot=result.project,
    )


@router.post(
    "/api/projects/{project_id}/panels/{panel_id}/generate-image",
    response_model=ProjectMutationResponse[PanelImageGenerationResult],
    responses=GENERATION_ERROR_RESPONSES,
)
async def generate_panel_image(
    project_id: str,
    panel_id: str,
    request: Request,
    revision: int,
    payload: Annotated[GenerationJobCreate | None, Body()] = None,
) -> ProjectMutationResponse[PanelImageGenerationResult]:
    request.app.state.mutation.require_revision(project_id, revision)
    if request.app.state.generation.find_active_panel_job(project_id, panel_id):
        raise HTTPException(status_code=409, detail="このコマは画像生成中です")
    candidate_count = payload.candidate_count if payload else 1
    jobs = request.app.state.generation.start(
        project_id,
        [panel_id],
        candidate_count,
        "画像生成ジョブを登録しました",
        expected_revision=revision,
    )
    job = jobs[0]
    ensure_sync_generation_succeeded(await request.app.state.generation.await_completion(job))
    completed_snapshot = request.app.state.mutation.current_snapshot(project_id)
    return to_project_mutation_response(
        request,
        project_id,
        PanelImageGenerationResult(panel_id=panel_id),
        snapshot=completed_snapshot,
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
    response_model=ProjectMutationResponse[GenerationJobResponse],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def create_generation_job(
    project_id: str,
    panel_id: str,
    request: Request,
    revision: int,
    payload: Annotated[GenerationJobCreate | None, Body()] = None,
) -> ProjectMutationResponse[GenerationJobResponse]:
    request.app.state.mutation.require_revision(project_id, revision)
    if request.app.state.generation.find_active_panel_job(project_id, panel_id):
        raise HTTPException(status_code=409, detail="このコマは画像生成中です")
    jobs = request.app.state.generation.start(
        project_id,
        [panel_id],
        payload.candidate_count if payload else 1,
        "画像生成ジョブを登録しました",
        expected_revision=revision,
    )
    job = jobs[0]
    return to_project_mutation_response(
        request, project_id, to_job_response(job), snapshot=jobs.project
    )


@router.post(
    "/api/projects/{project_id}/generation-jobs",
    response_model=ProjectMutationResponse[BatchGenerationJobResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def create_batch_generation_jobs(
    project_id: str,
    request: Request,
    revision: int,
    payload: Annotated[BatchGenerationJobCreate | None, Body()] = None,
) -> ProjectMutationResponse[BatchGenerationJobResult]:
    manga = request.app.state.mutation.require_revision(project_id, revision).manga
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
    # 自動候補数: 見せ場・複数人物コマだけ増やし、candidate_countを下限として尊重する。
    candidate_counts = None
    if options.auto_candidates:
        candidate_counts = {
            panel.panel_id: max(options.candidate_count, suggest_candidate_count(panel))
            for panel in panels
        }
    jobs = request.app.state.generation.start(
        project_id,
        target_ids,
        options.candidate_count,
        "一括生成キューへ登録しました",
        skip_active=True,
        expected_revision=revision,
        candidate_counts=candidate_counts,
    )
    return to_project_mutation_response(
        request,
        project_id,
        BatchGenerationJobResult(jobs=[to_job_response(job) for job in jobs]),
        snapshot=jobs.project,
    )


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


@router.post(
    "/api/generation-jobs/{job_id}/cancel",
    response_model=ProjectMutationResponse[GenerationJobResponse],
)
async def cancel_generation_job(
    job_id: str, request: Request
) -> ProjectMutationResponse[GenerationJobResponse]:
    job = get_job_or_404(request, job_id)
    cancelled = await request.app.state.generation.cancel(job)
    snapshot = request.app.state.mutation.current_snapshot(cancelled.project_id)
    return to_project_mutation_response(
        request, cancelled.project_id, to_job_response(cancelled), snapshot=snapshot
    )


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
    response_model=ProjectMutationResponse[PanelPageRenderResult],
    responses=CANDIDATE_SELECT_ERROR_RESPONSES,
)
def select_panel_candidate(
    project_id: str,
    panel_id: str,
    candidate_id: str,
    request: Request,
    revision: int,
) -> ProjectMutationResponse[PanelPageRenderResult]:
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

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=select
    )
    page_number = mutation_result.result
    try:
        rendered = request.app.state.rendering.render_and_commit_page(
            project_id, mutation_result.project.manga, mutation_result.project.revision, page_number
        )
    except (RenderCommitConflictError, RenderInputChangedError) as exc:
        # 候補採用は既にCAS確定済み。通常のrevision競合として返すと「未保存編集は適用
        # されませんでした」と誤認されるため、採用済みstateを持つ部分成功として返す。
        raise ProjectMutationPartiallyAppliedError(
            project_id,
            completed_operation="candidate_selection",
            failed_operation="render_page",
            snapshot=mutation_result.project,
        ) from exc
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
    "/api/projects/{project_id}/panels/{panel_id}/use-stub",
    response_model=ProjectMutationResponse[PanelImageGenerationResult],
    responses=PROJECT_MUTATION_ERROR_RESPONSES,
)
async def use_stub_panel_image(
    project_id: str,
    panel_id: str,
    request: Request,
    revision: int,
) -> ProjectMutationResponse[PanelImageGenerationResult]:
    checked = request.app.state.mutation.require_revision(project_id, revision)
    panel = find_panel(checked.manga, panel_id)
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

    mutation_result = request.app.state.mutation.mutate_user(
        project_id, expected_revision=revision, mutate=add
    )
    return to_project_mutation_response(
        request,
        project_id,
        PanelImageGenerationResult(panel_id=panel_id),
        snapshot=mutation_result.project,
    )
