from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from asyncio import CancelledError
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import ValidationError

from . import fonts as font_registry
from . import knowledge as knowledge_db
from . import layout_engine
from . import preflight as preflight_module
from . import story as story_module
from .assets import (
    normalize_manga_assets,
    path_to_asset_id,
    resolve_asset_path,
    safe_component,
    stable_asset_name,
)
from .config import Settings
from .database import (
    KnowledgeChunkRecord,
    KnowledgeSourceRecord,
    ProjectRecord,
    ProjectRevisionRecord,
    StoryGenerationSessionRecord,
    create_session_factory,
    now_utc,
)
from .generation_service import ActiveJobConflictError, GenerationService, PanelNotFoundError
from .generator import generate_four_page_name
from .image_backends import StubImageBackend, build_image_backend, get_comfyui_status
from .jobs import TERMINAL_JOB_STATUSES, GenerationJob, JobManager
from .llm import build_llm_client, get_llm_status
from .mutation import (
    EpochMismatchError,
    InvalidProjectJsonError,
    ProjectConflictError,
    ProjectMutationService,
    ProjectNotFoundError,
    mark_page_dirty,
)
from .prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from .renderer import (
    export_cbz,
    render_project_page,
    render_project_pages,
    sanitize_export_filename,
)
from .schemas import (
    ApiErrorResponse,
    BatchGenerationJobCreate,
    BatchGenerationJobResponse,
    CharacterReferenceResponse,
    ComfyUIStatusResponse,
    ExportResponse,
    FontInfo,
    FontsResponse,
    GenerateNameRequest,
    GenerationJobCreate,
    GenerationJobResponse,
    ImageCandidate,
    KnowledgeChunkResponse,
    KnowledgeDocumentRequest,
    KnowledgeImportRequest,
    KnowledgeImportResponse,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceResponse,
    LayoutSuggestRequest,
    LayoutSuggestResponse,
    LLMStatusResponse,
    LocalKnowledgeSyncResponse,
    LocalKnowledgeWorkResponse,
    MangaProject,
    OpenExportFolderResponse,
    PageProductionStatus,
    PageRenderResponse,
    PanelControlReference,
    PanelImageGenerationResponse,
    PanelPageRenderResponse,
    PreflightIssue,
    PreflightResponse,
    ProjectCreate,
    ProjectDetail,
    ProjectProductionStatus,
    ProjectRevisionResponse,
    ProjectSummary,
    PromptPreviewResponse,
    ReferenceAssetResponse,
    RenderRequest,
    RenderResponse,
    StageGenerateRequest,
    StageUpdateRequest,
    StorySessionCreate,
    StorySessionResponse,
    StorySessionSummary,
)
from .story import StoryError

logger = logging.getLogger(__name__)

# 同期生成API(generate-image / render)の追加エラー契約をOpenAPIへ明示する。
# 実行時にキャンセルは409、生成バックエンド失敗は502を返す。
GENERATION_ERROR_RESPONSES: dict[int | str, dict] = {
    409: {
        "model": ApiErrorResponse,
        "description": "生成がキャンセルされた（入力変更・構成置換など）",
    },
    502: {"model": ApiErrorResponse, "description": "画像生成バックエンドが失敗した"},
}


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_settings.export_dir.mkdir(parents=True, exist_ok=True)
        app_settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        Path("data").mkdir(parents=True, exist_ok=True)
        app.state.settings = app_settings
        app.state.SessionLocal = create_session_factory(app_settings.database_url)
        app.state.mutation = ProjectMutationService(app.state.SessionLocal, app_settings.export_dir)
        app.state.generation = GenerationService(app.state.SessionLocal, app_settings.export_dir)
        app.state.job_manager = JobManager(app.state.SessionLocal)
        to_start, interrupted = app.state.job_manager.restore_pending()
        # クラッシュ復旧で中断扱いにしたジョブは、対応panelのgeneration.statusもerrorへ同期する
        # （ジョブ履歴だけerrorでManga JSON側がrunning表示のまま残らないように）。
        for job in interrupted:
            mark_panel_job_stopped(
                app,
                job,
                "バックエンド再起動により中断されました。必要なら再実行してください",
                error=True,
            )
        for job in to_start:
            app.state.job_manager.start(job, run_generation_job(app, job))
        try:
            yield
        finally:
            await app.state.job_manager.shutdown()

    app = FastAPI(title="Local Doujin Studio", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/fonts", response_model=FontsResponse)
    def list_fonts() -> FontsResponse:
        fonts = [FontInfo(**item) for item in font_registry.list_fonts()]
        path = font_registry.find_dialogue_font_path()
        primary = font_registry.dialogue_font_is_primary()
        return FontsResponse(
            dialogue_font=("源暎アンチック" if primary else (path.name if path else "PIL既定")),
            dialogue_font_available=path is not None,
            fonts=fonts,
        )

    @app.get("/api/fonts/dialogue/file")
    def get_dialogue_font() -> FileResponse:
        path = font_registry.find_dialogue_font_path()
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="写植用フォントが見つかりません")
        return FileResponse(path, media_type="font/ttf", filename=path.name)

    @app.get("/api/comfyui/status", response_model=ComfyUIStatusResponse)
    async def comfyui_status(request: Request) -> ComfyUIStatusResponse:
        status = await get_comfyui_status(request.app.state.settings)
        return ComfyUIStatusResponse(**status.__dict__)

    @app.post("/api/projects", response_model=ProjectDetail)
    def create_project(payload: ProjectCreate, request: Request) -> ProjectDetail:
        project_id = str(uuid.uuid4())
        manga = MangaProject(
            title=payload.title, work_name=payload.work_name, target_pages=payload.target_pages
        )
        record = ProjectRecord(
            id=project_id,
            title=payload.title,
            work_name=payload.work_name,
            manga_json=manga.model_dump_json(),
            created_at=now_utc(),
            updated_at=now_utc(),
        )
        with request.app.state.SessionLocal() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        return to_detail(record, request.app.state.settings.export_dir)

    @app.get("/api/projects", response_model=list[ProjectSummary])
    def list_projects(request: Request) -> list[ProjectSummary]:
        with request.app.state.SessionLocal() as session:
            records = session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all()
        return [to_summary(record) for record in records]

    @app.get("/api/projects/{project_id}", response_model=ProjectDetail)
    def get_project(project_id: str, request: Request) -> ProjectDetail:
        record = load_project_record(request, project_id)
        return to_detail(record, request.app.state.settings.export_dir)

    @app.post("/api/projects/{project_id}/generate-name", response_model=ProjectDetail)
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
        replace_project(
            request.app.state.mutation,
            project_id,
            manga,
            expected_revision=payload.revision,
            increment_epoch=True,
        )
        new_epoch = request.app.state.mutation.current_epoch(project_id)
        await cancel_project_jobs_before_epoch(request.app, project_id, new_epoch)
        return to_detail(load_project_record(request, project_id))

    @app.put("/api/projects/{project_id}/manga-json", response_model=ProjectDetail)
    async def update_manga_json(
        project_id: str,
        payload: MangaProject,
        request: Request,
        revision: int,
    ) -> ProjectDetail:
        # 全文置換はrevision必須。未指定の無条件保存は古いクライアントJSONで
        # サーバの最新内容（生成候補・他編集）を巻き戻せてしまうため許可しない。
        record = load_project_record(request, project_id)
        previous = parse_manga_json(record.manga_json)
        structure_changed = structure_signature(payload) != structure_signature(previous)
        # 描画入力が変わったページだけ未レンダリングに戻す（メタ変更では戻さない）。
        invalidate_changed_pages(payload, previous)
        replace_project(
            request.app.state.mutation,
            project_id,
            payload,
            expected_revision=revision,
            increment_epoch=structure_changed,
        )
        if structure_changed:
            new_epoch = request.app.state.mutation.current_epoch(project_id)
            await cancel_project_jobs_before_epoch(request.app, project_id, new_epoch)
        return to_detail(
            load_project_record(request, project_id), request.app.state.settings.export_dir
        )

    @app.post(
        "/api/projects/{project_id}/render",
        response_model=RenderResponse,
        responses=GENERATION_ERROR_RESPONSES,
    )
    async def render_project(
        project_id: str,
        request: Request,
        payload: RenderRequest | None = Body(default=None),
    ) -> RenderResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        settings = request.app.state.settings
        force = payload.force if payload else False
        manager: JobManager = request.app.state.job_manager
        # /render開始時の世代を固定する。途中でネーム再生成・ストーリー適用・全文構造変更が
        # 起きたら、古い/render要求が新しい作品構成へ生成・確定し続けないよう409で止める。
        started_epoch = record.generation_epoch

        def ensure_same_epoch() -> None:
            if request.app.state.mutation.current_epoch(project_id) != started_epoch:
                raise HTTPException(
                    status_code=409,
                    detail="レンダリング中に作品構成が更新されました。最新で再実行してください。",
                )

        # ComfyUI呼び出しはジョブワーカーに一本化する。ここでは生成ジョブを積んで完了を待つ。
        for page in manga.pages:
            for panel in page.panels:
                if not (force or not panel.image_asset):
                    continue
                ensure_same_epoch()
                job = find_active_panel_job(manager, project_id, panel.panel_id)
                if job is None:
                    # expected_epochで、構成置換後の新作品へジョブを積むのを防ぐ（不一致は409）。
                    job = enqueue_panel_jobs(
                        request.app,
                        project_id,
                        [panel.panel_id],
                        1,
                        "全体生成ジョブを登録しました",
                        expected_epoch=started_epoch,
                    )[0]
                    manager.start(job, run_generation_job(request.app, job))
                ensure_generation_succeeded(await await_job(request.app, job))
                ensure_same_epoch()

        ensure_same_epoch()
        latest = load_project_record(request, project_id)
        snapshot = parse_manga_json(latest.manga_json)
        # 採用candidateとassetが不整合なpanel（選択済みなのにasset欠損等）があれば、
        # プレースホルダを完成画像として確定しないよう、PNG公開前に409で中止する。
        inconsistent = find_inconsistent_selected_panel(snapshot, settings.export_dir)
        if inconsistent is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{inconsistent.panel_id}: 採用画像が欠損/不整合です。"
                    "再選択または再生成してから出力してください。"
                ),
            )
        # publish開始からcommitまで一つのtry/exceptで囲い、途中失敗でも公開済みPNGを回収する。
        published_by_request: dict[Path, bool] = {}
        try:
            page_assets, warnings = render_snapshot_pages(
                project_id,
                snapshot,
                settings.export_dir,
                latest.revision,
                ownership=published_by_request,
            )
            # 確定も開始時epoch条件のCASにし、構成置換後のdone確定を防ぐ。
            manga, revision = commit_rendered_pages(
                request.app, project_id, snapshot, page_assets, expected_epoch=started_epoch
            )
        except Exception:
            cleanup_published_assets(request.app, project_id, published_by_request)
            raise
        return RenderResponse(
            project_id=project_id,
            page_assets=[asset_to_id(path, settings.export_dir) for path in page_assets],
            manga_json=manga,
            revision=revision,
            warnings=warnings,
        )

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id}/generate-image",
        response_model=PanelImageGenerationResponse,
        responses=GENERATION_ERROR_RESPONSES,
    )
    async def generate_panel_image(
        project_id: str,
        panel_id: str,
        request: Request,
        payload: GenerationJobCreate | None = Body(default=None),
    ) -> PanelImageGenerationResponse:
        manager: JobManager = request.app.state.job_manager
        if find_active_panel_job(manager, project_id, panel_id):
            raise HTTPException(status_code=409, detail="このコマは画像生成中です")
        # 同期レスポンスが欲しい直接生成APIも、ジョブを作って完了待ちする薄いラッパーにする。
        candidate_count = payload.candidate_count if payload else 1
        # panel queued化・job登録・revision更新を単一トランザクションで確定する。
        job = enqueue_panel_jobs(
            request.app, project_id, [panel_id], candidate_count, "画像生成ジョブを登録しました"
        )[0]
        manager.start(job, run_generation_job(request.app, job))
        ensure_generation_succeeded(await await_job(request.app, job))
        latest = load_project_record(request, project_id)
        return PanelImageGenerationResponse(
            project_id=project_id,
            panel_id=panel_id,
            manga_json=parse_manga_json(latest.manga_json),
            revision=latest.revision,
        )

    @app.get(
        "/api/projects/{project_id}/panels/{panel_id}/prompt-preview",
        response_model=PromptPreviewResponse,
    )
    def preview_panel_prompt(
        project_id: str, panel_id: str, request: Request
    ) -> PromptPreviewResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        positive, negative = compose_panel_prompts(manga, panel)
        return PromptPreviewResponse(
            panel_id=panel_id,
            positive_prompt=positive,
            negative_prompt=negative,
            character_ids=panel.characters,
        )

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id}/generation-jobs",
        response_model=GenerationJobResponse,
    )
    async def create_generation_job(
        project_id: str,
        panel_id: str,
        request: Request,
        payload: GenerationJobCreate | None = Body(default=None),
    ) -> GenerationJobResponse:
        manager: JobManager = request.app.state.job_manager
        if find_active_panel_job(manager, project_id, panel_id):
            raise HTTPException(status_code=409, detail="このコマは画像生成中です")
        candidate_count = payload.candidate_count if payload else 1
        job = enqueue_panel_jobs(
            request.app, project_id, [panel_id], candidate_count, "画像生成ジョブを登録しました"
        )[0]
        manager.start(job, run_generation_job(request.app, job))
        return to_job_response(job)

    @app.post(
        "/api/projects/{project_id}/generation-jobs", response_model=BatchGenerationJobResponse
    )
    async def create_batch_generation_jobs(
        project_id: str,
        request: Request,
        payload: BatchGenerationJobCreate | None = Body(default=None),
    ) -> BatchGenerationJobResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        options = payload or BatchGenerationJobCreate()
        panels = [
            panel
            for page in manga.pages
            if options.page is None or page.page == options.page
            for panel in page.panels
        ]
        if not panels:
            raise HTTPException(status_code=404, detail="生成対象のコマが見つかりません")
        manager: JobManager = request.app.state.job_manager
        active_keys = {
            (item.project_id, item.panel_id)
            for item in manager.jobs.values()
            if item.status not in TERMINAL_JOB_STATUSES
        }
        target_ids = [
            panel.panel_id for panel in panels if (project_id, panel.panel_id) not in active_keys
        ]
        if not target_ids:
            raise HTTPException(status_code=409, detail="対象コマはすべて生成中です")
        jobs = enqueue_panel_jobs(
            request.app,
            project_id,
            target_ids,
            options.candidate_count,
            "一括生成キューへ登録しました",
            skip_active=True,
        )
        for job in jobs:
            manager.start(job, run_generation_job(request.app, job))
        return BatchGenerationJobResponse(jobs=[to_job_response(job) for job in jobs])

    @app.get("/api/generation-jobs/{job_id}", response_model=GenerationJobResponse)
    def get_generation_job(job_id: str, request: Request) -> GenerationJobResponse:
        job = get_job_or_404(request, job_id)
        return to_job_response(job)

    @app.get(
        "/api/projects/{project_id}/generation-jobs", response_model=list[GenerationJobResponse]
    )
    def list_generation_jobs(project_id: str, request: Request) -> list[GenerationJobResponse]:
        load_project_record(request, project_id)
        manager: JobManager = request.app.state.job_manager
        return [to_job_response(job) for job in manager.list_for_project(project_id)]

    @app.post("/api/generation-jobs/{job_id}/cancel", response_model=GenerationJobResponse)
    async def cancel_generation_job(job_id: str, request: Request) -> GenerationJobResponse:
        manager: JobManager = request.app.state.job_manager
        job = get_job_or_404(request, job_id)
        # 状態遷移を先に確定する。完了直後(done/error)や既にcancelledなら何もしない。
        # これで成功済みコマがskipped表示になる回帰を防ぐ。
        prompt_id = job.prompt_id
        if not manager.cancel(job):
            return to_job_response(job)
        # Taskが一度も実行されずCancelledError handlerへ入らない場合も、panelを必ず解放する。
        mark_panel_job_stopped(request.app, job, "生成をキャンセルしました")
        # 遷移できたときだけリモート停止とパネル状態更新を行う。
        # HTTP停止だけ別スレッドへ逃がし、JobManager操作はイベントループ側で完結させる。
        remote = await asyncio.to_thread(
            stop_comfyui_generation, request.app.state.settings, prompt_id
        )
        if remote == "failed":
            # ローカルではキャンセル済みだがリモート停止に失敗した状態を区別して伝える。
            manager.update(
                job,
                status="cancelled",
                message="ローカルではキャンセルしましたが、ComfyUI側の停止に失敗しました",
            )
        # API側で即時解放済み。Task側の冪等handlerも完了させてから応答する。
        task = manager.tasks.get(job.id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return to_job_response(job)

    @app.websocket("/api/generation-jobs/{job_id}/ws")
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

    @app.post(
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

        # CAS mutatorは純粋なJSON変更だけ行う。レンダリング(副作用)は確定manga/revisionに対し
        # commit後に1回だけ実行する（リトライでの多重書き込みを避ける）。
        def select(manga: MangaProject) -> int:
            panel = find_panel(manga, panel_id)
            candidate = next(
                (item for item in panel.image_candidates if item.id == candidate_id), None
            )
            if candidate is None:
                raise HTTPException(status_code=404, detail="画像候補が見つかりません")
            apply_candidate_selection(panel, candidate)
            page_number = find_panel_page_number(manga, panel_id)
            page = next(item for item in manga.pages if item.page == page_number)
            mark_page_dirty(page)
            return page_number

        page_number, manga, revision = run_mutation(request.app.state.mutation, project_id, select)
        page_asset, warnings, manga, revision = render_and_commit_page(
            request.app, project_id, manga, revision, page_number
        )
        return PanelPageRenderResponse(
            project_id=project_id,
            panel_id=panel_id,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
            revision=revision,
            warnings=warnings,
        )

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id}/use-stub",
        response_model=PanelImageGenerationResponse,
    )
    async def use_stub_panel_image(
        project_id: str,
        panel_id: str,
        request: Request,
    ) -> PanelImageGenerationResponse:
        record = load_project_record(request, project_id)
        panel = find_panel(parse_manga_json(record.manga_json), panel_id)
        settings = request.app.state.settings
        # 候補ごとに一意の保存先を使う。共有パスだと選び直しても画像が上書き済みになる。
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
            # 採用画像が変わるため、対象ページを再レンダリング対象へ戻す。
            if page is not None:
                mark_page_dirty(page)

        _result, manga, revision = run_mutation(request.app.state.mutation, project_id, add)
        return PanelImageGenerationResponse(
            project_id=project_id, panel_id=panel_id, manga_json=manga, revision=revision
        )

    @app.post(
        "/api/projects/{project_id}/characters/{character_id:path}/reference-image",
        response_model=CharacterReferenceResponse,
    )
    async def upload_character_reference(
        project_id: str,
        character_id: str,
        request: Request,
    ) -> CharacterReferenceResponse:
        record = load_project_record(request, project_id)
        manga0 = parse_manga_json(record.manga_json)
        if not any(item.id == character_id for item in manga0.characters):
            raise HTTPException(status_code=404, detail="キャラクターが見つかりません")
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "references"
            / stable_asset_name(character_id, "character")
        )
        await save_request_image(request, target)
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)

        def mutate(manga: MangaProject) -> None:
            character = next((item for item in manga.characters if item.id == character_id), None)
            if character is None:
                raise HTTPException(status_code=404, detail="キャラクターが見つかりません")
            character.reference_image_asset = asset_id

        _result, manga, revision = run_mutation(request.app.state.mutation, project_id, mutate)
        return CharacterReferenceResponse(
            character_id=character_id,
            asset=asset_id,
            manga_json=manga,
            revision=revision,
        )

    @app.post(
        "/api/projects/{project_id}/locations/{location_id:path}/reference-image",
        response_model=ReferenceAssetResponse,
    )
    async def upload_location_reference(
        project_id: str,
        location_id: str,
        request: Request,
    ) -> ReferenceAssetResponse:
        record = load_project_record(request, project_id)
        manga0 = parse_manga_json(record.manga_json)
        if not any(item.id == location_id for item in manga0.locations):
            raise HTTPException(status_code=404, detail="ロケーションが見つかりません")
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "locations"
            / stable_asset_name(location_id, "location")
        )
        await save_request_image(request, target)
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)

        def mutate(manga: MangaProject) -> None:
            location = next((item for item in manga.locations if item.id == location_id), None)
            if location is None:
                raise HTTPException(status_code=404, detail="ロケーションが見つかりません")
            location.reference_image_asset = asset_id

        _result, manga, revision = run_mutation(request.app.state.mutation, project_id, mutate)
        return ReferenceAssetResponse(
            target_id=location_id, asset=asset_id, manga_json=manga, revision=revision
        )

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id:path}/controls/{kind}/reference-image",
        response_model=ReferenceAssetResponse,
    )
    async def upload_panel_control_reference(
        project_id: str,
        panel_id: str,
        kind: str,
        request: Request,
        load_node_id: str,
    ) -> ReferenceAssetResponse:
        if kind not in {"pose", "depth", "lineart", "background"}:
            raise HTTPException(status_code=422, detail="Control参照種別が不正です")
        record = load_project_record(request, project_id)
        find_panel(parse_manga_json(record.manga_json), panel_id)  # 事前に存在確認(404)
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "controls"
            / stable_asset_name(panel_id, "panel").removesuffix(".png")
            / f"{kind}.png"
        )
        await save_request_image(request, target)
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)
        new_control_id = str(uuid.uuid4())

        def mutate(manga: MangaProject) -> str:
            panel = find_panel(manga, panel_id)
            existing = next((item for item in panel.control_references if item.kind == kind), None)
            if existing:
                existing.asset = asset_id
                existing.load_node_id = load_node_id
                return existing.id
            control = PanelControlReference(
                id=new_control_id, kind=kind, asset=asset_id, load_node_id=load_node_id
            )
            panel.control_references.append(control)
            return control.id

        target_id, manga, revision = run_mutation(request.app.state.mutation, project_id, mutate)
        return ReferenceAssetResponse(
            target_id=target_id, asset=asset_id, manga_json=manga, revision=revision
        )

    @app.post(
        "/api/projects/{project_id}/pages/{page_number}/overlays/{overlay_id:path}/{asset_kind}",
        response_model=ReferenceAssetResponse,
    )
    async def upload_overlay_asset(
        project_id: str,
        page_number: int,
        overlay_id: str,
        asset_kind: str,
        request: Request,
    ) -> ReferenceAssetResponse:
        if asset_kind not in {"asset", "mask"}:
            raise HTTPException(status_code=422, detail="overlayアセット種別が不正です")
        record = load_project_record(request, project_id)
        manga0 = parse_manga_json(record.manga_json)
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
        target = await save_content_addressed_request_image(
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
            # overlay画像/maskの差し替えは描画に影響するため、ページを再レンダリング対象へ。
            mark_page_dirty(page)

        _result, manga, revision = run_mutation(request.app.state.mutation, project_id, mutate)
        return ReferenceAssetResponse(
            target_id=overlay_id, asset=asset_id, manga_json=manga, revision=revision
        )

    @app.post(
        "/api/projects/{project_id}/pages/{page_number}/layout/suggest",
        response_model=LayoutSuggestResponse,
    )
    def suggest_page_layout(
        project_id: str,
        page_number: int,
        request: Request,
        payload: LayoutSuggestRequest | None = Body(default=None),
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
            # コマ枠(bbox)が変わるため、対象ページを再レンダリング対象へ戻す。
            mark_page_dirty(page)
            return page.layout_family

        layout_family, manga, revision = run_mutation(
            request.app.state.mutation, project_id, relayout
        )
        return LayoutSuggestResponse(
            project_id=project_id,
            page=page_number,
            layout_family=layout_family,
            manga_json=manga,
            revision=revision,
        )

    @app.post(
        "/api/projects/{project_id}/pages/{page_number}/preflight",
        response_model=PreflightResponse,
    )
    def preflight_page_endpoint(
        project_id: str,
        page_number: int,
        request: Request,
        payload: MangaProject | None = Body(default=None),
    ) -> PreflightResponse:
        # 本文でManga JSONが渡されれば非破壊で検査する（保存せずレンダリング状態を維持）。
        if payload is not None:
            manga = payload
        else:
            record = load_project_record(request, project_id)
            manga = parse_manga_json(record.manga_json)
        page = next((item for item in manga.pages if item.page == page_number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="ページが見つかりません")
        issues = preflight_module.preflight_page(
            manga, page, export_dir=request.app.state.settings.export_dir
        )
        return _to_preflight_response(project_id, page_number, issues)

    @app.post("/api/projects/{project_id}/preflight", response_model=PreflightResponse)
    def preflight_project_endpoint(project_id: str, request: Request) -> PreflightResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        issues = preflight_module.preflight_project(manga, request.app.state.settings.export_dir)
        return _to_preflight_response(project_id, None, issues)

    @app.post(
        "/api/projects/{project_id}/pages/{page_number}/render",
        response_model=PageRenderResponse,
    )
    def render_page_endpoint(
        project_id: str, page_number: int, request: Request
    ) -> PageRenderResponse:
        settings = request.app.state.settings

        record = load_project_record(request, project_id)
        snapshot = parse_manga_json(record.manga_json)
        if not any(page.page == page_number for page in snapshot.pages):
            raise HTTPException(status_code=404, detail="ページが見つかりません")
        page_asset, warnings, manga, revision = render_and_commit_page(
            request.app, project_id, snapshot, record.revision, page_number
        )
        page = next(item for item in manga.pages if item.page == page_number)
        issues = preflight_module.preflight_page(manga, page, export_dir=settings.export_dir)
        return PageRenderResponse(
            project_id=project_id,
            page=page_number,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
            revision=revision,
            warnings=warnings,
            preflight=_to_preflight_response(project_id, page_number, issues),
        )

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id}/render-page",
        response_model=PanelPageRenderResponse,
    )
    def render_panel_page(
        project_id: str, panel_id: str, request: Request
    ) -> PanelPageRenderResponse:
        settings = request.app.state.settings

        record = load_project_record(request, project_id)
        snapshot = parse_manga_json(record.manga_json)
        page_number = find_panel_page_number(snapshot, panel_id)
        page_asset, warnings, manga, revision = render_and_commit_page(
            request.app, project_id, snapshot, record.revision, page_number
        )
        return PanelPageRenderResponse(
            project_id=project_id,
            panel_id=panel_id,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
            revision=revision,
            warnings=warnings,
        )

    @app.post("/api/projects/{project_id}/export/cbz", response_model=ExportResponse)
    def export_project_cbz(project_id: str, request: Request) -> ExportResponse:
        settings = request.app.state.settings

        # preflight・production statusはCAS各試行で読んだ「確定する最新manga」に対して判定する
        # （古いスナップショットで検査して、競合で入った重大エラーを見逃さないように）。
        record = load_project_record(request, project_id)
        snapshot = parse_manga_json(record.manga_json)
        preflight_errors = [
            issue
            for issue in preflight_module.preflight_project(snapshot, settings.export_dir)
            if issue.level == "error"
        ]
        if preflight_errors:
            raise HTTPException(
                status_code=422,
                detail="プリフライトで重大エラーが見つかりました: "
                + "; ".join(
                    f"{issue.page}ページ {issue.message}" for issue in preflight_errors[:10]
                ),
            )
        # 採用candidateとassetが不整合なpanelがあれば、プレースホルダ入りCBZを出さない。
        inconsistent = find_inconsistent_selected_panel(snapshot, settings.export_dir)
        if inconsistent is not None:
            raise HTTPException(
                status_code=422,
                detail=f"{inconsistent.panel_id}: 採用画像が欠損/不整合です。CBZ出力前に修正してください。",
            )
        status = build_production_status(project_id, snapshot, settings.export_dir)
        blockers = [blocker for blocker in status.blockers if "採用画像" in blocker]
        # CBZ完成まではDBをdoneへ進めない。失敗時は全ページpendingのまま残る。
        # publish開始からcommitまで一つのtry/exceptで囲い、PNG公開後・CBZ生成中に
        # 例外が起きても、このリクエストで公開した不変アセットを必ず回収する。
        published_by_request: dict[Path, bool] = {}
        try:
            page_assets, render_warnings = render_snapshot_pages(
                project_id,
                snapshot,
                settings.export_dir,
                record.revision,
                ownership=published_by_request,
            )
            cbz_path = export_confirmed_cbz(
                project_id,
                snapshot.title,
                page_assets,
                settings.export_dir,
                record.revision,
                ownership=published_by_request,
            )
            manga, revision = commit_rendered_pages(
                request.app,
                project_id,
                snapshot,
                page_assets,
                expected_revision=record.revision,
                expected_epoch=record.generation_epoch,
            )
        except Exception:
            cleanup_published_assets(request.app, project_id, published_by_request)
            raise
        return ExportResponse(
            project_id=project_id,
            cbz_asset=asset_to_id(cbz_path, settings.export_dir),
            absolute_path=str(cbz_path.resolve()),
            revision=revision,
            manga_json=manga,
            warnings=blockers + render_warnings,
        )

    @app.post(
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
        cbz_path = (
            max(cbz_files, key=lambda path: path.stat().st_mtime) if cbz_files else project_dir
        )
        open_in_file_manager(cbz_path)
        return OpenExportFolderResponse(
            project_id=project_id,
            folder_path=str(project_dir),
            cbz_path=str(cbz_path),
            cbz_exists=cbz_path.is_file(),
        )

    @app.get("/api/projects/{project_id}/production-status", response_model=ProjectProductionStatus)
    def get_production_status(project_id: str, request: Request) -> ProjectProductionStatus:
        record = load_project_record(request, project_id)
        return build_production_status(
            project_id,
            parse_manga_json(record.manga_json),
            request.app.state.settings.export_dir,
        )

    @app.get("/api/llm/status", response_model=LLMStatusResponse)
    async def llm_status(request: Request) -> LLMStatusResponse:
        status = await get_llm_status(request.app.state.settings)
        return LLMStatusResponse(**status.__dict__)

    # --- 作品知識DB ---

    @app.get("/api/knowledge/local-works", response_model=list[LocalKnowledgeWorkResponse])
    def list_local_knowledge_works(request: Request) -> list[LocalKnowledgeWorkResponse]:
        try:
            works = knowledge_db.list_local_works(request.app.state.settings.knowledge_dir)
        except knowledge_db.LocalKnowledgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [
            LocalKnowledgeWorkResponse(
                work_id=work.work_id,
                work_name=work.work_name,
                description=work.description,
                document_count=len(work.documents),
            )
            for work in works
        ]

    @app.post(
        "/api/knowledge/local-works/{work_id}/sync",
        response_model=LocalKnowledgeSyncResponse,
    )
    def sync_local_knowledge_work(work_id: str, request: Request) -> LocalKnowledgeSyncResponse:
        try:
            work = knowledge_db.load_local_work(request.app.state.settings.knowledge_dir, work_id)
            with request.app.state.SessionLocal() as session:
                records = knowledge_db.sync_local_work(session, work)
                sources = [to_knowledge_source(record) for record in records]
        except knowledge_db.LocalKnowledgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return LocalKnowledgeSyncResponse(
            work=LocalKnowledgeWorkResponse(
                work_id=work.work_id,
                work_name=work.work_name,
                description=work.description,
                document_count=len(work.documents),
            ),
            sources=sources,
        )

    @app.post("/api/knowledge/sources/import", response_model=KnowledgeImportResponse)
    def import_knowledge_sources(
        payload: KnowledgeImportRequest, request: Request
    ) -> KnowledgeImportResponse:
        sources: list[KnowledgeSourceResponse] = []
        with request.app.state.SessionLocal() as session:
            for file in payload.files:
                doc_type = knowledge_db.infer_doc_type(file.filename)
                record = knowledge_db.import_source(
                    session,
                    work_name=payload.work_name,
                    title=file.filename,
                    doc_type=doc_type,
                    usage=payload.usage,
                    content=file.content,
                )
                sources.append(to_knowledge_source(record))
            # import_sourceは内部commitを持たないため、まとめて1トランザクションで確定する。
            session.commit()
        return KnowledgeImportResponse(sources=sources)

    @app.post("/api/knowledge/documents", response_model=KnowledgeSourceResponse)
    def add_knowledge_document(
        payload: KnowledgeDocumentRequest, request: Request
    ) -> KnowledgeSourceResponse:
        with request.app.state.SessionLocal() as session:
            record = knowledge_db.import_source(
                session,
                work_name=payload.work_name,
                title=payload.title or "無題ドキュメント",
                doc_type=payload.doc_type,
                usage=payload.usage,
                content=payload.content,
            )
            session.commit()
            return to_knowledge_source(record)

    @app.get("/api/knowledge/sources", response_model=list[KnowledgeSourceResponse])
    def list_knowledge_sources(
        request: Request, work_name: str | None = None
    ) -> list[KnowledgeSourceResponse]:
        with request.app.state.SessionLocal() as session:
            return [
                to_knowledge_source(record)
                for record in knowledge_db.list_sources(session, work_name)
            ]

    @app.delete("/api/knowledge/sources/{source_id}")
    def delete_knowledge_source(source_id: str, request: Request) -> dict[str, bool]:
        with request.app.state.SessionLocal() as session:
            if not knowledge_db.delete_source(session, source_id):
                raise HTTPException(status_code=404, detail="知識ソースが見つかりません")
            session.commit()
        return {"ok": True}

    @app.post("/api/knowledge/search", response_model=KnowledgeSearchResponse)
    def search_knowledge(
        payload: KnowledgeSearchRequest, request: Request
    ) -> KnowledgeSearchResponse:
        with request.app.state.SessionLocal() as session:
            hits = knowledge_db.search_chunks(
                session,
                work_name=payload.work_name,
                query=payload.query,
                usage=payload.usage,
                limit=payload.limit,
            )
            return KnowledgeSearchResponse(
                hits=[
                    KnowledgeSearchHit(chunk=to_knowledge_chunk(record), score=score, method=method)
                    for record, score, method in hits
                ]
            )

    # --- ストーリー生成セッション ---

    @app.post("/api/projects/{project_id}/story-sessions", response_model=StorySessionResponse)
    def create_story_session(
        project_id: str, payload: StorySessionCreate, request: Request
    ) -> StorySessionResponse:
        record = load_project_record(request, project_id)
        work_name = payload.work_name or record.work_name
        if payload.knowledge_work_id:
            with request.app.state.SessionLocal() as session:
                try:
                    local_work = knowledge_db.load_local_work(
                        request.app.state.settings.knowledge_dir, payload.knowledge_work_id
                    )
                    knowledge_db.sync_local_work(session, local_work)
                except knowledge_db.LocalKnowledgeError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            work_name = local_work.work_name

            def update_work_name(manga: MangaProject) -> None:
                manga.work_name = work_name

            # knowledge同期後のproject更新もCAS経由にし、生成完了などとの競合を
            # 古い全文で上書きしない。変更対象はwork_nameだけなので競合時は再適用できる。
            run_mutation(request.app.state.mutation, project_id, update_work_name)
        with request.app.state.SessionLocal() as session:
            story_record = story_module.create_session(
                session,
                project_id=project_id,
                work_name=work_name,
                target_pages=payload.target_pages,
                instruction=payload.instruction,
            )
            return story_module.session_to_response(story_record)

    @app.get("/api/projects/{project_id}/story-sessions", response_model=list[StorySessionSummary])
    def list_story_sessions(project_id: str, request: Request) -> list[StorySessionSummary]:
        with request.app.state.SessionLocal() as session:
            records = (
                session.query(StoryGenerationSessionRecord)
                .filter(StoryGenerationSessionRecord.project_id == project_id)
                .order_by(StoryGenerationSessionRecord.created_at.desc())
                .all()
            )
            return [to_story_summary(record) for record in records]

    @app.get("/api/story-sessions/{session_id}", response_model=StorySessionResponse)
    def get_story_session(session_id: str, request: Request) -> StorySessionResponse:
        with request.app.state.SessionLocal() as session:
            record = session.get(StoryGenerationSessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            return story_module.session_to_response(record)

    @app.post(
        "/api/story-sessions/{session_id}/stages/{stage}/generate",
        response_model=StorySessionResponse,
    )
    async def generate_story_stage(
        session_id: str,
        stage: str,
        request: Request,
        payload: StageGenerateRequest | None = Body(default=None),
    ) -> StorySessionResponse:
        settings = request.app.state.settings
        llm = build_llm_client(settings)
        with request.app.state.SessionLocal() as session:
            record = session.get(StoryGenerationSessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            try:
                await story_module.generate_stage(
                    session, llm, settings, record, stage, payload.instruction if payload else ""
                )
            except StoryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            return story_module.session_to_response(record)

    @app.put("/api/story-sessions/{session_id}/stages/{stage}", response_model=StorySessionResponse)
    def update_story_stage(
        session_id: str, stage: str, payload: StageUpdateRequest, request: Request
    ) -> StorySessionResponse:
        with request.app.state.SessionLocal() as session:
            record = session.get(StoryGenerationSessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            try:
                story_module.update_stage(session, record, stage, payload.data)
            except StoryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            return story_module.session_to_response(record)

    @app.post(
        "/api/story-sessions/{session_id}/stages/{stage}/approve",
        response_model=StorySessionResponse,
    )
    def approve_story_stage(session_id: str, stage: str, request: Request) -> StorySessionResponse:
        with request.app.state.SessionLocal() as session:
            record = session.get(StoryGenerationSessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            try:
                story_module.approve_stage(session, record, stage)
            except StoryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            return story_module.session_to_response(record)

    @app.post("/api/story-sessions/{session_id}/apply", response_model=ProjectDetail)
    async def apply_story_session(
        session_id: str, request: Request, revision: int
    ) -> ProjectDetail:
        with request.app.state.SessionLocal() as session:
            record = session.get(StoryGenerationSessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            project_id = record.project_id

        def build_applied(session, base: MangaProject) -> MangaProject:
            current = session.get(StoryGenerationSessionRecord, session_id)
            if current is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            try:
                return story_module.apply_session(session, current, base)
            except StoryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        replace_project_with_history(
            request.app.state.mutation,
            project_id,
            build_applied,
            expected_revision=revision,
            history_label=f"ストーリー適用前 ({now_utc().isoformat()})",
        )
        new_epoch = request.app.state.mutation.current_epoch(project_id)
        await cancel_project_jobs_before_epoch(request.app, project_id, new_epoch)
        return to_detail(
            load_project_record(request, project_id), request.app.state.settings.export_dir
        )

    @app.get("/api/projects/{project_id}/revisions", response_model=list[ProjectRevisionResponse])
    def list_project_revisions(project_id: str, request: Request) -> list[ProjectRevisionResponse]:
        load_project_record(request, project_id)
        with request.app.state.SessionLocal() as session:
            records = (
                session.query(ProjectRevisionRecord)
                .filter(ProjectRevisionRecord.project_id == project_id)
                .order_by(ProjectRevisionRecord.created_at.desc())
                .all()
            )
            return [
                ProjectRevisionResponse(
                    id=record.id,
                    project_id=record.project_id,
                    label=record.label,
                    created_at=record.created_at,
                )
                for record in records
            ]

    @app.post(
        "/api/projects/{project_id}/revisions/{revision_id}/restore", response_model=ProjectDetail
    )
    async def restore_project_revision(
        project_id: str, revision_id: str, request: Request, revision: int
    ) -> ProjectDetail:
        load_project_record(request, project_id)

        def build_restored(session, _base: MangaProject) -> MangaProject:
            stored = session.get(ProjectRevisionRecord, revision_id)
            if stored is None or stored.project_id != project_id:
                raise HTTPException(status_code=404, detail="リビジョンが見つかりません")
            return story_module.restore_revision(stored.manga_json)

        replace_project_with_history(
            request.app.state.mutation,
            project_id,
            build_restored,
            expected_revision=revision,
            history_label=f"復元前 ({now_utc().isoformat()})",
        )
        new_epoch = request.app.state.mutation.current_epoch(project_id)
        await cancel_project_jobs_before_epoch(request.app, project_id, new_epoch)
        return to_detail(
            load_project_record(request, project_id), request.app.state.settings.export_dir
        )

    @app.get("/api/assets/{asset_id:path}")
    def get_asset(asset_id: str, request: Request) -> FileResponse:
        try:
            target = resolve_asset_path(asset_id, request.app.state.settings.export_dir)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="アセットが見つかりません") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="アセットが見つかりません")
        return FileResponse(target)

    return app


async def await_job(app: FastAPI, job: GenerationJob) -> GenerationJob:
    """既に開始済みのジョブの完了を待つ（ComfyUI呼び出しはワーカー側に一本化）。"""
    manager: JobManager = app.state.job_manager
    task = manager.tasks.get(job.id)
    if task is not None:
        # run_generation_jobは例外を内部で握って状態に反映するため、ここでは伝播させない。
        await asyncio.gather(task, return_exceptions=True)
    return manager.get(job.id) or job


def ensure_generation_succeeded(job: GenerationJob) -> None:
    """同期完了を要求するendpointでは失敗・キャンセルを成功レスポンスにしない。"""
    if job.status == "done":
        return
    if job.status == "cancelled":
        raise HTTPException(status_code=409, detail=job.message)
    raise HTTPException(status_code=502, detail=job.message)


async def cancel_project_jobs_before_epoch(app: FastAPI, project_id: str, new_epoch: int) -> None:
    """構造置換成功後、旧epochのローカルTaskとComfyUI promptだけを停止する。"""
    manager: JobManager = app.state.job_manager
    cancelled_tasks: list[asyncio.Task] = []
    remote_cancels = []
    cancelled_jobs: list[GenerationJob] = []
    for item in list(manager.jobs.values()):
        if not (
            item.project_id == project_id
            and item.epoch < new_epoch
            and item.status not in TERMINAL_JOB_STATUSES
        ):
            continue
        prompt_id = item.prompt_id
        if not manager.cancel(item):
            continue
        mark_panel_job_stopped(app, item, "生成をキャンセルしました")
        cancelled_jobs.append(item)
        remote_cancels.append(
            asyncio.to_thread(stop_comfyui_generation, app.state.settings, prompt_id)
        )
        task = manager.tasks.get(item.id)
        if task is not None:
            cancelled_tasks.append(task)
    if remote_cancels:
        results = await asyncio.gather(*remote_cancels, return_exceptions=True)
        for job, result in zip(cancelled_jobs, results, strict=True):
            if result == "failed" or isinstance(result, Exception):
                manager.update(
                    job,
                    message="作品構成変更により停止しましたが、ComfyUI側の停止に失敗しました",
                )
    if cancelled_tasks:
        await asyncio.gather(*cancelled_tasks, return_exceptions=True)


def find_active_panel_job(
    manager: JobManager, project_id: str, panel_id: str
) -> GenerationJob | None:
    return next(
        (
            item
            for item in manager.jobs.values()
            if item.project_id == project_id
            and item.panel_id == panel_id
            and item.status not in TERMINAL_JOB_STATUSES
        ),
        None,
    )


# リモート(ComfyUI)停止結果の区分。UI表示にも使う。
RemoteCancelState = Literal["not_requested", "queued_removed", "interrupted", "failed"]


def stop_comfyui_generation(settings: Settings, prompt_id: str | None) -> RemoteCancelState:
    """ComfyUI側の対象生成だけを停止する。

    /interruptはprompt_idを指定できない「現在実行中の処理を止める」グローバル操作なので、
    対象promptが実行中だと確認できたときだけ使う。キュー待ちならprompt_id指定でqueueから
    削除する。対象が見当たらなければ無関係な生成を巻き込まないよう何もしない。
    """
    if settings.image_backend.lower() != "comfyui" or not prompt_id:
        return "not_requested"
    base = settings.comfyui_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{base}/queue")
            response.raise_for_status()
            queue = response.json()
            running = {entry[1] for entry in queue.get("queue_running", []) if len(entry) > 1}
            pending = {entry[1] for entry in queue.get("queue_pending", []) if len(entry) > 1}
            if prompt_id in pending:
                deleted = client.post(f"{base}/queue", json={"delete": [prompt_id]})
                deleted.raise_for_status()
                return "queued_removed"
            if prompt_id in running:
                interrupted = client.post(f"{base}/interrupt")
                interrupted.raise_for_status()
                return "interrupted"
            # 対象がキューに無い（完了済み/別物）。グローバルinterruptは避ける。
            return "not_requested"
    except Exception:
        return "failed"


async def run_generation_job(app: FastAPI, job: GenerationJob) -> None:
    manager: JobManager = app.state.job_manager
    manager.update(job, message="生成キューで待機中です")
    try:
        async with manager.generation_lock:
            await execute_generation_job(app, job)
    except CancelledError:
        # API側で即時解放できなかった経路（shutdown等）と競合時の冪等な保険。
        # job状態はHTTPキャンセルのmanager.cancel()またはshutdown()が既に確定している。
        if manager.shutting_down:
            mark_panel_job_stopped(
                app,
                job,
                "バックエンド停止により中断されました。必要なら再実行してください",
                error=True,
            )
        else:
            mark_panel_job_stopped(app, job, "生成をキャンセルしました")
        raise


async def execute_generation_job(app: FastAPI, job: GenerationJob) -> None:
    manager: JobManager = app.state.job_manager
    # このジョブが生成した候補PNG。epoch/入力不一致での破棄時に未参照分を回収する。
    # 最初のepochチェックで例外が出てもexceptで参照できるよう、try外で初期化する。
    candidate_assets: dict[Path, bool] = {}
    try:

        def set_running(panel, page) -> None:
            panel.generation.status = "running"
            panel.generation.message = "画像候補を生成中です"

        # 最新を読み直して対象パネルだけ更新し、並行編集を踏みつぶさない。
        # 構成全置換後（世代不一致）なら、ここで破棄して候補を新作品へ混ぜない。
        manga = update_panel_in_latest(
            app, job.project_id, job.panel_id, set_running, expected_epoch=job.epoch
        )
        panel = find_panel(manga, job.panel_id)
        prepared_panel = prepare_panel_for_generation(manga, panel)
        input_hash = generation_input_hash(manga, panel, app.state.settings.export_dir)
        manager.update(
            job,
            status="running",
            generation_input_hash=input_hash,
            message="画像候補を生成中です",
        )
        backend = build_image_backend(app.state.settings)
        # 開始時の選択状態を記録し、生成中にユーザーが選び直したら自動選択で上書きしない。
        selection_state = {
            "base": panel.selected_candidate_id,
            "auto": panel.selected_candidate_id,
            "allow": True,
        }

        for candidate_index in range(job.candidate_count):
            candidate_id = str(uuid.uuid4())
            generated_panel = prepared_panel.model_copy(deep=True)
            generated_panel.generation.seed += candidate_index
            target = (
                app.state.settings.export_dir
                / job.project_id
                / "panels"
                / job.panel_id
                / f"{candidate_id}.png"
            )
            # 生成前に所有権を記録する。CancelledError/キャンセル/不一致のいずれでも、
            # 採用されなかったPNGを未参照判定で確実に回収できるようにする。
            candidate_assets[target.resolve()] = True

            async def report_progress(
                current: int,
                total: int,
                node: str | None,
                message: str,
                candidate_number: int = candidate_index,
            ) -> None:
                fraction = current / max(total, 1)
                overall = round(((candidate_number + fraction) / job.candidate_count) * 100)
                manager.update(
                    job,
                    progress=max(0, min(overall, 99)),
                    current=current,
                    total=total,
                    node=node,
                    message=f"候補 {candidate_number + 1}/{job.candidate_count}: {message}",
                )

            async def store_prompt_id(prompt_id: str) -> None:
                manager.update(job, prompt_id=prompt_id)

            result = await backend.generate_panel(
                job.project_id,
                generated_panel,
                app.state.settings.export_dir,
                target_path=target,
                progress_callback=report_progress,
                on_prompt_id=store_prompt_id,
            )
            # backendが別パスへ書いた場合も所有権へ含める（通常はtargetと同一）。
            if result.asset_path is not None:
                candidate_assets[Path(result.asset_path).resolve()] = True
            # キャンセル要求後に生成物が返っても、対象ジョブがキャンセル済みなら保存しない。
            # PNGを回収し、panel状態もskippedへ確定する（runningのまま固定されると、
            # 次回enqueueでactive扱いになり再生成不能になるため）。
            if job.status == "cancelled":
                cleanup_published_assets(app, job.project_id, candidate_assets)
                mark_panel_job_stopped(app, job, "生成をキャンセルしました")
                return
            if result.asset_path is None:
                raise RuntimeError("生成画像の保存先が返りませんでした")
            candidate = ImageCandidate(
                id=candidate_id,
                asset=str(result.asset_path),
                backend=result.backend,
                status=result.status,
                prompt=generated_panel.generation.prompt or generated_panel.prompt,
                negative_prompt=generated_panel.generation.negative_prompt,
                characters=list(generated_panel.characters),
                loras=list(generated_panel.generation.loras),
                reference_images=list(generated_panel.generation.reference_images),
                workflow_preset=generated_panel.generation.workflow_preset,
                seed=generated_panel.generation.seed,
                prompt_id=result.prompt_id,
                message=result.message,
                created_at=now_utc(),
            )

            def add_candidate_to_latest(
                latest: MangaProject, new_candidate: ImageCandidate = candidate
            ) -> bool:
                target_panel, target_page = find_panel_and_page(latest, job.panel_id)
                if target_panel is None:
                    raise RuntimeError("コマが見つかりません")
                latest_hash = generation_input_hash(
                    latest, target_panel, app.state.settings.export_dir
                )
                if latest_hash != job.generation_input_hash:
                    owned_candidate_ids = set(job.candidate_ids)
                    target_panel.image_candidates = [
                        item
                        for item in target_panel.image_candidates
                        if item.id not in owned_candidate_ids
                    ]
                    if target_panel.selected_candidate_id in owned_candidate_ids:
                        target_panel.selected_candidate_id = None
                        target_panel.image_asset = None
                    if owned_candidate_ids and target_page is not None:
                        mark_page_dirty(target_page)
                    target_panel.generation.status = "pending"
                    target_panel.generation.prompt_id = None
                    target_panel.generation.message = (
                        "生成中に入力が変更されたため、古い候補を破棄しました"
                    )
                    return False
                target_panel.image_candidates.append(new_candidate)
                current = target_panel.selected_candidate_id
                # 開始時の選択、またはジョブ自身が直前に自動選択した状態から変わっていなければ
                # 新候補を自動選択する。生成中にユーザーが別候補を選んでいたら追加のみに留める。
                if selection_state["allow"] and current in (
                    selection_state["base"],
                    selection_state["auto"],
                    None,
                ):
                    apply_candidate_selection(target_panel, new_candidate)
                    selection_state["auto"] = new_candidate.id
                    # 採用画像が変わったので、対象ページを再レンダリング対象へ戻す。
                    if target_page is not None:
                        mark_page_dirty(target_page)
                else:
                    selection_state["allow"] = False
                return True

            # 候補ごとに最新を読み直してマージし、生成中のユーザー編集を残す。
            # 構成全置換後（世代不一致）なら候補を保存せず破棄する（EpochMismatchError）。
            stored, _latest, _revision = app.state.mutation.mutate(
                job.project_id,
                add_candidate_to_latest,
                expected_epoch=job.epoch,
            )
            if not stored:
                raise GenerationInputMismatchError()
            job.candidate_ids.append(candidate_id)
            manager.update(
                job,
                progress=round(((candidate_index + 1) / job.candidate_count) * 100),
                # 次候補投入前にprompt_idをクリアし、古いidでのリモート停止誤爆を防ぐ。
                prompt_id=None,
                message=f"候補 {candidate_index + 1}/{job.candidate_count} を保存しました",
            )

        manager.update(
            job,
            status="done",
            progress=100,
            node=None,
            prompt_id=None,
            message="画像候補の生成が完了しました",
        )
    except CancelledError:
        # API/Task双方からのpanel更新はmark_panel_job_stopped()の冪等性へ委ねる。
        # 採用されなかった生成PNGはここで回収する（current/history参照分は残る）。
        cleanup_published_assets(app, job.project_id, candidate_assets)
        raise
    except EpochMismatchError:
        # ネーム再生成・ストーリー適用・復元で作品構成が置き換わった。古いプロンプトの
        # 候補を新作品へ混ぜないよう、保存せずジョブをキャンセル扱いにする。
        cleanup_published_assets(app, job.project_id, candidate_assets)
        manager.update(
            job,
            status="cancelled",
            node=None,
            prompt_id=None,
            message="作品構成が変わったため生成を破棄しました",
        )
    except GenerationInputMismatchError:
        # 破棄した候補のPNG本体も、current/history未参照なら回収する。
        cleanup_published_assets(app, job.project_id, candidate_assets)
        manager.update(
            job,
            status="cancelled",
            node=None,
            prompt_id=None,
            message="生成入力が変わったため古い候補を破棄しました",
        )
    except Exception as exc:
        # backendがPNGを書いた後に例外化した場合の未参照PNGを回収する。
        cleanup_published_assets(app, job.project_id, candidate_assets)
        if job.status == "cancelled":
            mark_panel_job_stopped(app, job, "生成をキャンセルしました")
            return
        mark_panel_job_stopped(app, job, f"画像候補の生成に失敗しました: {exc}", error=True)
        manager.update(
            job,
            status="error",
            node=None,
            prompt_id=None,
            message=f"画像候補の生成に失敗しました: {exc}",
        )


def mark_panel_job_stopped(
    app: FastAPI, job: GenerationJob, message: str, error: bool = False
) -> None:
    desired_status = "error" if error else "skipped"
    # API側とTask側の両方から呼ばれるため、同一状態ならCAS更新自体を省略する。
    try:
        with app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, job.project_id)
            if record is None or record.generation_epoch != job.epoch:
                return
            current = find_panel_optional(parse_manga_json(record.manga_json), job.panel_id)
            if (
                current is not None
                and current.generation.status == desired_status
                and current.generation.message == message
            ):
                return
    except Exception:
        return

    def mutate(panel, page) -> None:
        panel.generation.status = desired_status
        panel.generation.message = message

    try:
        # 構造全置換後に古いTaskのキャンセル処理が到着しても、新しい同名panelの
        # generation状態を書き換えない。
        update_panel_in_latest(app, job.project_id, job.panel_id, mutate, expected_epoch=job.epoch)
    except Exception:
        return


def apply_candidate_selection(panel, candidate: ImageCandidate) -> None:
    panel.selected_candidate_id = candidate.id
    panel.image_asset = candidate.asset
    panel.generation.backend = candidate.backend
    panel.generation.status = candidate.status
    panel.generation.seed = candidate.seed
    panel.generation.prompt_id = candidate.prompt_id
    panel.generation.message = candidate.message


def get_job_or_404(request: Request, job_id: str) -> GenerationJob:
    manager: JobManager = request.app.state.job_manager
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="生成ジョブが見つかりません")
    return job


def to_job_response(job: GenerationJob) -> GenerationJobResponse:
    return GenerationJobResponse(**job.as_dict())


def _to_preflight_response(
    project_id: str, page: int | None, issues: list[PreflightIssue]
) -> PreflightResponse:
    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [issue for issue in issues if issue.level == "warning"]
    return PreflightResponse(
        project_id=project_id,
        page=page,
        ok=not errors,
        errors=errors,
        warnings=warnings,
    )


def build_production_status(
    project_id: str, manga: MangaProject, export_dir: Path
) -> ProjectProductionStatus:
    page_statuses: list[PageProductionStatus] = []
    project_blockers: list[str] = []
    adopted_total = 0
    panel_total = 0
    rendered_pages = 0
    for page in manga.pages:
        total = len(page.panels)
        # 採用画像は「選択済み かつ assetが整合してファイルが存在する」ものだけ数える。
        adopted = sum(
            1 for panel in page.panels if selected_panel_asset_is_valid(panel, export_dir)
        )
        rendered = page_render_is_valid(manga, page, export_dir)
        blockers: list[str] = []
        for panel in page.panels:
            if not panel.selected_candidate_id:
                blockers.append(f"{panel.panel_id}: 採用画像が未選択です")
            elif not selected_panel_asset_is_valid(panel, export_dir):
                blockers.append(f"{panel.panel_id}: 採用画像が欠損しています")
        if not rendered:
            blockers.append(f"{page.page}ページ: ページが未レンダリングです")
        if adopted == total and rendered:
            status = "complete"
        elif adopted == total:
            status = "ready"
        else:
            status = "incomplete"
        page_statuses.append(
            PageProductionStatus(
                page=page.page,
                status=status,
                adopted_panels=adopted,
                total_panels=total,
                rendered=rendered,
                blockers=blockers,
            )
        )
        adopted_total += adopted
        panel_total += total
        rendered_pages += int(rendered)
        project_blockers.extend(blockers)
    if page_statuses and all(page.status == "complete" for page in page_statuses):
        project_status = "complete"
    elif page_statuses and all(page.status in {"ready", "complete"} for page in page_statuses):
        project_status = "ready"
    else:
        project_status = "incomplete"
    return ProjectProductionStatus(
        project_id=project_id,
        status=project_status,
        adopted_panels=adopted_total,
        total_panels=panel_total,
        rendered_pages=rendered_pages,
        total_pages=len(manga.pages),
        pages=page_statuses,
        blockers=project_blockers,
    )


def load_project_record(request: Request, project_id: str) -> ProjectRecord:
    with request.app.state.SessionLocal() as session:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        session.expunge(record)
    manga = parse_manga_json(record.manga_json)
    export_dir = request.app.state.settings.export_dir
    if migrate_legacy_render_state(manga, export_dir):
        # 旧形式のdoneを安全側へ戻す。CAS競合時は最新へ同じ移行を再適用する。
        run_mutation(
            request.app.state.mutation,
            project_id,
            lambda latest: migrate_legacy_render_state(latest, export_dir),
        )
        with request.app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, project_id)
            if record is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            session.expunge(record)
        manga = parse_manga_json(record.manga_json)
    record.manga_json = normalize_manga_assets(
        manga, request.app.state.settings.export_dir
    ).model_dump_json()
    return record


def migrate_legacy_render_state(manga: MangaProject, export_dir: Path) -> bool:
    """不変render assetが欠損・不整合なdoneページをpendingへ移行する。"""
    changed = False
    for page in manga.pages:
        if page.render_status == "done" and not page_render_is_valid(manga, page, export_dir):
            mark_page_dirty(page)
            changed = True
    return changed


def selected_panel_asset_is_valid(panel, export_dir: Path) -> bool:
    """採用candidateと実際に描画するimage_assetが整合し、ファイルが存在するか。

    generation_epochは構造変更しか検出しないため、候補選択・asset消失・候補削除などの
    非構造編集ではプレースホルダを完成画像として確定し得る。これを明示的に弾く。
    """
    if not panel.selected_candidate_id or not panel.image_asset:
        return False
    candidate = next(
        (item for item in panel.image_candidates if item.id == panel.selected_candidate_id),
        None,
    )
    if candidate is None or panel.image_asset != candidate.asset:
        return False
    try:
        return resolve_asset_path(panel.image_asset, export_dir).is_file()
    except ValueError:
        return False


def find_inconsistent_selected_panel(manga: MangaProject, export_dir: Path):
    """採用candidate指定済みなのにassetが欠損/不整合なpanelを返す（無ければNone）。"""
    for page in manga.pages:
        inconsistent = find_inconsistent_selected_panel_for_page(manga, page.page, export_dir)
        if inconsistent is not None:
            return inconsistent
    return None


def find_inconsistent_selected_panel_for_page(
    manga: MangaProject, page_number: int, export_dir: Path
):
    """対象ページ内で、採用candidate指定済みなのにassetが欠損/不整合なpanelを返す。"""
    page = next((item for item in manga.pages if item.page == page_number), None)
    if page is None:
        return None
    for panel in page.panels:
        if panel.selected_candidate_id and not selected_panel_asset_is_valid(panel, export_dir):
            return panel
    return None


def page_render_is_valid(manga: MangaProject, page, export_dir: Path) -> bool:
    if page.render_status != "done" or not page.render_asset or not page.render_hash:
        return False
    if page.render_hash != page_render_hash(manga, page):
        return False
    expected_name = f"page_{page.page:03d}.{page.render_hash}.png"
    try:
        path = resolve_asset_path(page.render_asset, export_dir)
    except ValueError:
        return False
    return path.name == expected_name and path.is_file()


class RenderInputChangedError(Exception):
    pass


class GenerationInputMismatchError(Exception):
    pass


def generation_input_hash(manga: MangaProject, panel, export_dir: Path) -> str:
    """実際にbackendへ渡す生成入力と参照画像内容から安定hashを作る。"""
    generated = prepare_panel_for_generation(manga, panel)
    payload = generated.model_dump(
        exclude={
            "image_asset",
            "image_candidates",
            "selected_candidate_id",
            "dialogue",
            "sfx",
        }
    )
    generation = payload["generation"]
    for volatile in ("status", "message", "prompt_id"):
        generation.pop(volatile, None)
    asset_ids = {reference.asset for reference in generated.control_references} | {
        reference.asset for reference in generated.generation.reference_images
    }
    asset_digests: dict[str, str] = {}
    for asset_id in sorted(asset_ids):
        try:
            path = resolve_asset_path(asset_id, export_dir)
            asset_digests[asset_id] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            )
        except (OSError, ValueError):
            asset_digests[asset_id] = "missing"
    payload["asset_digests"] = asset_digests
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def render_snapshot_page(
    project_id: str,
    manga: MangaProject,
    page_number: int,
    export_dir: Path,
    revision: int,
    ownership: dict[Path, bool] | None = None,
) -> tuple[Path, list[str]]:
    """snapshotを一時描画し、入力hash付き不変PNGへ昇格する。DB状態は変更しない。"""
    staging = (
        export_dir / project_id / ".render-staging" / f"revision-{revision}-{uuid.uuid4().hex}"
    )
    try:
        staged, warnings = render_project_page(
            project_id, manga, page_number, export_dir, output_dir=staging
        )
        page = next(item for item in manga.pages if item.page == page_number)
        render_hash = page_render_hash(manga, page)
        target = export_dir / project_id / "pages" / f"page_{page_number:03d}.{render_hash}.png"
        created = publish_immutable_asset(staged, target)
        if ownership is not None:
            ownership[target] = created
        return target, warnings
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def render_snapshot_pages(
    project_id: str,
    manga: MangaProject,
    export_dir: Path,
    revision: int,
    ownership: dict[Path, bool] | None = None,
) -> tuple[list[Path], list[str]]:
    """全ページ成功後に入力hash付き不変PNG群へ昇格する。DB状態は変更しない。"""
    staging = (
        export_dir / project_id / ".render-staging" / f"revision-{revision}-{uuid.uuid4().hex}"
    )
    try:
        staged_assets, warnings = render_project_pages(
            project_id, manga, export_dir, output_dir=staging
        )
        target_dir = export_dir / project_id / "pages"
        target_dir.mkdir(parents=True, exist_ok=True)
        assets: list[Path] = []
        pages = {page.page: page for page in manga.pages}
        for staged in staged_assets:
            page_number = int(staged.stem.removeprefix("page_"))
            render_hash = page_render_hash(manga, pages[page_number])
            target = target_dir / f"page_{page_number:03d}.{render_hash}.png"
            created = publish_immutable_asset(staged, target)
            if ownership is not None:
                ownership[target] = created
            assets.append(target)
        return assets, warnings
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def export_confirmed_cbz(
    project_id: str,
    title: str,
    page_assets: list[Path],
    export_dir: Path,
    revision: int,
    ownership: dict[Path, bool] | None = None,
) -> Path:
    """CBZを確定revisionの一時パスで完成させてから正規出力へ昇格する。"""
    staging = export_dir / project_id / ".cbz-staging" / f"revision-{revision}-{uuid.uuid4().hex}"
    try:
        staged = export_cbz(project_id, title, page_assets, export_dir, output_dir=staging)
        manifest_hash = hashlib.sha256(
            "\n".join(asset.name for asset in sorted(page_assets)).encode("utf-8")
        ).hexdigest()[:16]
        target = (
            export_dir
            / project_id
            / (
                f"{sanitize_export_filename(title)}-r{revision}-{manifest_hash}-"
                f"{uuid.uuid4().hex}.cbz"
            )
        )
        created = publish_immutable_asset(staged, target)
        if ownership is not None:
            ownership[target] = created
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def publish_immutable_asset(staged: Path, target: Path) -> bool:
    """既存成果物を上書きせず、同内容なら再利用する。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, target)
        created = True
    except FileExistsError as exc:
        if (
            hashlib.sha256(staged.read_bytes()).digest()
            != hashlib.sha256(target.read_bytes()).digest()
        ):
            raise RuntimeError(f"不変アセットの内容が一致しません: {target.name}") from exc
        created = False
    finally:
        staged.unlink(missing_ok=True)
    return created


def iter_manga_asset_strings(manga: MangaProject):
    """Manga JSON内の「アセットを指すフィールド」だけを列挙する。

    prompt・台詞・作品名など任意文字列をパス候補にすると、長文で
    OSError(File name too long)を誘発し、cleanupを巻き込んでジョブを停止不能にする。
    そのためassetフィールドを明示的に収集する。
    """
    for character in manga.characters:
        if character.reference_image_asset:
            yield character.reference_image_asset
    for location in manga.locations:
        if location.reference_image_asset:
            yield location.reference_image_asset
    for page in manga.pages:
        if page.render_asset:
            yield page.render_asset
        for overlay in page.overlay_elements:
            if overlay.asset:
                yield overlay.asset
            if overlay.mask_asset:
                yield overlay.mask_asset
        for panel in page.panels:
            if panel.image_asset:
                yield panel.image_asset
            for control in panel.control_references:
                if control.asset:
                    yield control.asset
            for reference in panel.generation.reference_images:
                if reference.asset:
                    yield reference.asset
            for candidate in panel.image_candidates:
                if candidate.asset:
                    yield candidate.asset
                for reference in candidate.reference_images:
                    if reference.asset:
                        yield reference.asset


def referenced_project_asset_paths(app: FastAPI, project_id: str) -> set[Path]:
    with app.state.SessionLocal() as session:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            return set()
        raw_documents = [record.manga_json]
        raw_documents.extend(
            manga_json
            for (manga_json,) in session.query(ProjectRevisionRecord.manga_json)
            .filter(ProjectRevisionRecord.project_id == project_id)
            .all()
        )
    export_dir = app.state.settings.export_dir
    paths: set[Path] = set()
    for raw in raw_documents:
        try:
            manga = MangaProject.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            continue
        for value in iter_manga_asset_strings(manga):
            try:
                path = resolve_asset_path(value, export_dir)
            except ValueError:
                continue
            # 参照確認はcleanupの補助。is_fileのOSError等で本来の409/502を上書きしない。
            try:
                if path.is_file():
                    paths.add(path)
            except OSError:
                continue
    return paths


def cleanup_published_assets(app: FastAPI, project_id: str, ownership: dict[Path, bool]) -> None:
    """この要求が作成し、current/historyのどちらからも未参照な成果物だけ回収する。

    referenced側は常にcanonical absolute path。ownership側は相対EXPORT_DIR下だと相対の
    ことがあるため、必ずresolve()して同じ基準で比較する（相対パスのまま比較すると
    参照中assetを誤って削除する）。
    """
    referenced = referenced_project_asset_paths(app, project_id)
    for path, created_by_request in ownership.items():
        target = path.resolve()
        if created_by_request and target not in referenced:
            # cleanupは補助処理。unlink失敗で本来の409/502を上書きしないよう吸収する。
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.warning("cleanup unlink失敗: %s", target, exc_info=True)


def commit_rendered_pages(
    app: FastAPI,
    project_id: str,
    snapshot: MangaProject,
    assets: list[Path],
    *,
    expected_revision: int | None = None,
    expected_epoch: int | None = None,
) -> tuple[MangaProject, int]:
    """最新入力がsnapshotと一致する場合だけdoneと不変assetをCAS確定する。"""
    snapshot_pages = {page.page: page for page in snapshot.pages}
    asset_by_page = {int(asset.name.split(".")[0].removeprefix("page_")): asset for asset in assets}

    def finalize(latest: MangaProject) -> None:
        latest_pages = {page.page: page for page in latest.pages}
        for page_number, snapshot_page in snapshot_pages.items():
            latest_page = latest_pages.get(page_number)
            asset = asset_by_page.get(page_number)
            if (
                latest_page is None
                or asset is None
                or page_render_hash(latest, latest_page)
                != page_render_hash(snapshot, snapshot_page)
            ):
                raise RenderInputChangedError()
        for page_number, snapshot_page in snapshot_pages.items():
            latest_page = latest_pages[page_number]
            latest_page.render_status = "done"
            latest_page.rendered_at = now_utc()
            latest_page.render_hash = page_render_hash(snapshot, snapshot_page)
            latest_page.render_asset = asset_to_id(
                asset_by_page[page_number], app.state.settings.export_dir
            )

    try:
        _result, manga, revision = app.state.mutation.mutate(
            project_id,
            finalize,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
        )
    except RenderInputChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail="描画中にページ内容が更新されました。再度レンダリングしてください。",
        ) from exc
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail="描画結果の確定中に競合しました") from exc
    except EpochMismatchError as exc:
        raise HTTPException(status_code=409, detail="作品構成が更新されています") from exc
    return manga, revision


def render_and_commit_page(
    app: FastAPI,
    project_id: str,
    snapshot: MangaProject,
    snapshot_revision: int,
    page_number: int,
) -> tuple[Path, list[str], MangaProject, int]:
    # 全体render・CBZと同じ不変条件をここに集約する。直接ページrender・候補選択後renderも
    # この経路を通るため、採用candidateとassetが不整合ならプレースホルダを確定せず409にする。
    inconsistent = find_inconsistent_selected_panel_for_page(
        snapshot, page_number, app.state.settings.export_dir
    )
    if inconsistent is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{inconsistent.panel_id}: 採用画像が欠損/不整合です。"
                "再選択または再生成してください。"
            ),
        )
    published_by_request: dict[Path, bool] = {}
    try:
        asset, warnings = render_snapshot_page(
            project_id,
            snapshot,
            page_number,
            app.state.settings.export_dir,
            snapshot_revision,
            ownership=published_by_request,
        )
        # 対象ページだけを確定するため、snapshotを対象ページへ絞ったCAS用projectにする。
        page = next(item for item in snapshot.pages if item.page == page_number)
        commit_snapshot = snapshot.model_copy(deep=True)
        commit_snapshot.pages = [page.model_copy(deep=True)]
        manga, revision = commit_rendered_pages(app, project_id, commit_snapshot, [asset])
    except Exception:
        cleanup_published_assets(app, project_id, published_by_request)
        raise
    return asset, warnings, manga, revision


# 1コマ画像として現実的な上限。展開後のピクセル数で「圧縮爆弾」を弾く。
MAX_IMAGE_PIXELS = 64_000_000  # 約8000x8000
MAX_IMAGE_DIMENSION = 12_000
# Pillow自体の圧縮爆弾検知も明示的に有効化する（巨大画像でDecompressionBombErrorを投げる）。
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


async def save_request_image(request: Request, target: Path, preserve_alpha: bool = False) -> None:
    content = await request.body()
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="参照画像は20MB以下にしてください")
    try:
        with Image.open(io.BytesIO(content)) as source:
            # 圧縮後が小さくても展開後に巨大化しうるため、ピクセル数・縦横を先に検査する。
            width, height = source.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise HTTPException(
                    status_code=422,
                    detail="画像サイズが大きすぎます（最大8000x8000・約64メガピクセル）",
                )
            # 透過オーバーフレーム（人物切り抜き等）はアルファを保持する。
            image = source.convert("RGBA" if preserve_alpha else "RGB")
    except HTTPException:
        raise
    except Image.DecompressionBombError as exc:
        raise HTTPException(status_code=422, detail="画像サイズが大きすぎます") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail="参照画像を読み込めません") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    # 検証済み画像を一時ファイルへ書き出し、成功後にreplaceで原子的に差し替える。
    temporary = target.with_suffix(target.suffix + ".tmp")
    image.save(temporary, format="PNG")
    temporary.replace(target)


async def save_content_addressed_request_image(
    request: Request,
    asset_dir: Path,
    asset_kind: str,
    *,
    preserve_alpha: bool = False,
) -> Path:
    """正規化済みPNGの内容hashを持つ不変assetとして保存する。"""
    temporary = asset_dir / f".{asset_kind}-{uuid.uuid4().hex}.png"
    await save_request_image(request, temporary, preserve_alpha=preserve_alpha)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    target = asset_dir / f"{asset_kind}-{digest}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(target)
    return target


def parse_manga_json(raw: str) -> MangaProject:
    try:
        return MangaProject.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"Manga JSONが不正です: {exc}") from exc


def find_panel(manga: MangaProject, panel_id: str):
    panel = find_panel_optional(manga, panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="コマが見つかりません")
    return panel


def find_panel_optional(manga: MangaProject, panel_id: str):
    panel, _page = find_panel_and_page(manga, panel_id)
    return panel


def find_panel_and_page(manga: MangaProject, panel_id: str):
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return panel, page
    return None, None


def page_render_signature(manga: MangaProject, page) -> str:
    """ページの描画結果に影響する入力だけを取り出した安定シグネチャ。

    一致する限り既存のレンダリング状態を保持し、台詞・レイアウトなど
    画像に影響しないメタ変更では再レンダリングを促さない。
    """
    payload = {
        "typography": manga.typography.model_dump(),
        "page_layout": manga.page_layout.model_dump(),
        "reading_direction": manga.reading_direction,
        "overlays": [overlay.model_dump() for overlay in page.overlay_elements],
        "panels": [
            {
                "panel_id": panel.panel_id,
                "bbox": panel.bbox,
                "image_asset": panel.image_asset,
                "dialogue": [line.model_dump() for line in panel.dialogue],
                "sfx": [item.model_dump() for item in panel.sfx],
                "crop": {
                    "fit_mode": panel.generation.fit_mode,
                    "crop_anchor": panel.generation.crop_anchor,
                    "crop_scale": panel.generation.crop_scale,
                    "crop_offset_x": panel.generation.crop_offset_x,
                    "crop_offset_y": panel.generation.crop_offset_y,
                    "focal_x": panel.generation.focal_x,
                    "focal_y": panel.generation.focal_y,
                },
            }
            for panel in page.panels
        ],
    }
    return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


def structure_signature(manga: MangaProject) -> tuple:
    """生成ジョブの意味論を変えるページ・コマ構造だけを比較する。"""
    return tuple(
        (
            page.page,
            tuple(panel.panel_id for panel in page.panels),
            tuple(page.reading_order),
        )
        for page in manga.pages
    )


def page_render_hash(manga: MangaProject, page) -> str:
    return hashlib.sha256(page_render_signature(manga, page).encode("utf-8")).hexdigest()[:20]


def invalidate_changed_pages(payload: MangaProject, previous: MangaProject) -> None:
    """描画入力が変わったページのみpendingにし、それ以外は前回状態を引き継ぐ。"""
    previous_by_number = {page.page: page for page in previous.pages}
    previous_signatures = {
        page.page: page_render_signature(previous, page) for page in previous.pages
    }
    for page in payload.pages:
        old_page = previous_by_number.get(page.page)
        if old_page is not None and previous_signatures.get(page.page) == page_render_signature(
            payload, page
        ):
            page.render_status = old_page.render_status
            page.rendered_at = old_page.rendered_at
            page.render_asset = old_page.render_asset
            page.render_hash = old_page.render_hash
        else:
            mark_page_dirty(page)


def run_mutation(service: ProjectMutationService, project_id: str, mutate, expected_revision=None):
    """ProjectMutationServiceを呼び、サービス例外をHTTPExceptionへ変換する。"""
    try:
        return service.mutate(project_id, mutate, expected_revision=expected_revision)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "他の操作（生成完了や別タブの保存）で更新されています。"
                "最新を読み込み直してください。"
            ),
        ) from exc
    except InvalidProjectJsonError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def replace_project(
    service: ProjectMutationService,
    project_id: str,
    manga: MangaProject,
    *,
    expected_revision: int,
    increment_epoch: bool = False,
) -> tuple[MangaProject, int]:
    try:
        return service.replace(
            project_id,
            manga,
            expected_revision=expected_revision,
            increment_epoch=increment_epoch,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="他の操作で更新されています。最新を読み込み直してください。",
        ) from exc
    except InvalidProjectJsonError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def replace_project_with_history(
    service: ProjectMutationService,
    project_id: str,
    build_replacement,
    *,
    expected_revision: int,
    history_label: str,
) -> tuple[MangaProject, int]:
    try:
        return service.replace_with_history(
            project_id,
            build_replacement,
            expected_revision=expected_revision,
            history_label=history_label,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="他の操作で更新されています。最新を読み込み直してください。",
        ) from exc
    except InvalidProjectJsonError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def update_panel_in_latest(
    app: FastAPI, project_id: str, panel_id: str, mutate, expected_epoch: int | None = None
) -> MangaProject:
    """最新のmanga_jsonを読み直し、対象パネルだけにmutateを適用してCAS保存する。

    生成完了時に開始時点の古いスナップショットを丸ごと保存すると、その間の
    ユーザー編集（他パネルの台詞・別ページの候補選択）を踏みつぶす。対象パネルだけ
    最新へマージし、競合時は読み直して再適用する（ProjectMutationService経由）。
    mutateは対象パネルと所属ページを受け取る（ページのdirty化に使う）。
    expected_epoch指定時は、構成全置換後（世代不一致）ならEpochMismatchErrorを送出する。
    """

    def manga_mutate(manga: MangaProject) -> None:
        panel, page = find_panel_and_page(manga, panel_id)
        if panel is None:
            raise RuntimeError("コマが見つかりません")
        mutate(panel, page)

    _result, manga, _revision = app.state.mutation.mutate(
        project_id, manga_mutate, expected_epoch=expected_epoch
    )
    return manga


def enqueue_panel_jobs(
    app: FastAPI,
    project_id: str,
    panel_ids: list[str],
    candidate_count: int,
    message: str,
    *,
    skip_active: bool = False,
    expected_epoch: int | None = None,
) -> list[GenerationJob]:
    """panelのqueued化・GenerationJobRecord追加・revision更新を単一トランザクションで確定する。

    別トランザクションに分けると、間でプロセス停止した際に「panelはqueuedだが対応ジョブが無い」
    という復旧不能な状態が残るため、同じSQLiteトランザクションで原子的に行う。
    commit後にメモリ登録し、Taskは呼び出し側がcommit確認後に開始する。
    """
    try:
        jobs = app.state.generation.enqueue(
            project_id,
            panel_ids,
            candidate_count,
            message,
            skip_active=skip_active,
            expected_epoch=expected_epoch,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except PanelNotFoundError as exc:
        raise HTTPException(status_code=404, detail="コマが見つかりません") from exc
    except (ActiveJobConflictError, EpochMismatchError) as exc:
        raise HTTPException(
            status_code=409, detail="対象コマまたは作品構成が更新されています"
        ) from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409, detail="ジョブ登録中に競合しました。再実行してください。"
        ) from exc
    manager: JobManager = app.state.job_manager
    for job in jobs:
        manager.register_in_memory(job)
    return jobs


def find_panel_page_number(manga: MangaProject, panel_id: str) -> int:
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return page.page
    raise HTTPException(status_code=404, detail="コマが見つかりません")


def apply_generation_result(panel, result) -> None:
    panel.image_asset = str(result.asset_path) if result.asset_path else None
    panel.generation.backend = result.backend
    panel.generation.status = result.status
    panel.generation.message = result.message
    panel.generation.prompt_id = result.prompt_id


def register_generation_candidate(panel, generated_panel, result, candidate_id=None) -> None:
    if result.asset_path is None:
        apply_generation_result(panel, result)
        return
    candidate = ImageCandidate(
        id=candidate_id or str(uuid.uuid4()),
        asset=str(result.asset_path),
        backend=result.backend,
        status=result.status,
        prompt=generated_panel.generation.prompt or generated_panel.prompt,
        negative_prompt=generated_panel.generation.negative_prompt,
        characters=list(generated_panel.characters),
        loras=list(generated_panel.generation.loras),
        reference_images=list(generated_panel.generation.reference_images),
        workflow_preset=generated_panel.generation.workflow_preset,
        seed=generated_panel.generation.seed,
        prompt_id=result.prompt_id,
        message=result.message,
        created_at=now_utc(),
    )
    panel.image_candidates.append(candidate)
    apply_candidate_selection(panel, candidate)


def to_knowledge_source(record: KnowledgeSourceRecord) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse(
        id=record.id,
        work_name=record.work_name,
        title=record.title,
        doc_type=record.doc_type,
        usage=record.usage,
        chunk_count=record.chunk_count,
        created_at=record.created_at,
    )


def to_knowledge_chunk(record: KnowledgeChunkRecord) -> KnowledgeChunkResponse:
    return KnowledgeChunkResponse(
        id=record.id,
        source_id=record.source_id,
        work_name=record.work_name,
        usage=record.usage,
        kind=record.kind,
        title=record.title,
        content=record.content,
        policy=record.policy,
        tags=[tag for tag in record.tags.split(", ") if tag],
        position=record.position,
    )


def to_story_summary(record: StoryGenerationSessionRecord) -> StorySessionSummary:
    return StorySessionSummary(
        id=record.id,
        project_id=record.project_id,
        work_name=record.work_name,
        target_pages=record.target_pages,
        instruction=record.instruction,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_summary(record: ProjectRecord) -> ProjectSummary:
    return ProjectSummary(
        id=record.id,
        title=record.title,
        work_name=record.work_name,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_detail(record: ProjectRecord, export_dir: Path | None = None) -> ProjectDetail:
    manga = parse_manga_json(record.manga_json)
    if export_dir is not None:
        normalize_manga_assets(manga, export_dir)
    return ProjectDetail(**to_summary(record).model_dump(), manga_json=manga)


def asset_to_id(path: Path, export_dir: Path) -> str:
    return path_to_asset_id(path, export_dir)


def open_in_file_manager(path: Path) -> None:
    target = path.resolve()
    if sys.platform == "win32":
        command = (
            ["explorer.exe", f"/select,{target}"]
            if target.is_file()
            else ["explorer.exe", str(target)]
        )
    elif sys.platform == "darwin":
        command = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
    else:
        command = ["xdg-open", str(target.parent if target.is_file() else target)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


app = create_app()
