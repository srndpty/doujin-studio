from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import uuid
from asyncio import CancelledError
from contextlib import asynccontextmanager
from pathlib import Path

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
from .generator import generate_four_page_name
from .image_backends import StubImageBackend, build_image_backend, get_comfyui_status
from .jobs import TERMINAL_JOB_STATUSES, GenerationJob, JobManager
from .llm import build_llm_client, get_llm_status
from .prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from .renderer import export_cbz, render_project_page, render_project_pages
from .schemas import (
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


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_settings.export_dir.mkdir(parents=True, exist_ok=True)
        app_settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        Path("data").mkdir(parents=True, exist_ok=True)
        app.state.settings = app_settings
        app.state.SessionLocal = create_session_factory(app_settings.database_url)
        app.state.job_manager = JobManager(app.state.SessionLocal)
        for job in app.state.job_manager.restore_pending():
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
    def generate_name(
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
        with request.app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, project_id)
            if record is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            record.work_name = payload.work_name
            record.manga_json = manga.model_dump_json()
            record.revision += 1
            record.updated_at = now_utc()
            session.commit()
            session.refresh(record)
        return to_detail(record)

    @app.put("/api/projects/{project_id}/manga-json", response_model=ProjectDetail)
    def update_manga_json(
        project_id: str,
        payload: MangaProject,
        request: Request,
        revision: int | None = None,
    ) -> ProjectDetail:
        normalize_manga_assets(payload, request.app.state.settings.export_dir)
        with request.app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, project_id)
            if record is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            # 楽観ロック: クライアントが読み込んだ時点より新しい更新があれば409にする。
            if revision is not None and revision != record.revision:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "他の操作（生成完了や別タブの保存）で更新されています。"
                        "最新を読み込み直してください。"
                    ),
                )
            previous = parse_manga_json(record.manga_json)
            # 描画入力が変わったページだけ未レンダリングに戻す（メタ変更では戻さない）。
            invalidate_changed_pages(payload, previous)
            record.title = payload.title
            record.work_name = payload.work_name
            record.manga_json = payload.model_dump_json()
            record.revision += 1
            record.updated_at = now_utc()
            session.commit()
            session.refresh(record)
        return to_detail(record, request.app.state.settings.export_dir)

    @app.post("/api/projects/{project_id}/render", response_model=RenderResponse)
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

        # ComfyUI呼び出しはジョブワーカーに一本化する。ここでは生成ジョブを積んで完了を待つ。
        for page in manga.pages:
            for panel in page.panels:
                if not (force or not panel.image_asset):
                    continue
                job = find_active_panel_job(manager, project_id, panel.panel_id)
                if job is None:
                    job = manager.create(project_id, panel.panel_id, 1)
                    manager.start(job, run_generation_job(request.app, job))
                await await_job(request.app, job)

        # 生成ジョブが最新manga_jsonへマージした結果を読み直してからレンダリングする。
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        page_assets, warnings = render_project_pages(project_id, manga, settings.export_dir)
        for page in manga.pages:
            page.render_status = "done"
            page.rendered_at = now_utc()
        save_manga_json(request, project_id, manga)
        return RenderResponse(
            project_id=project_id,
            page_assets=[asset_to_id(path, settings.export_dir) for path in page_assets],
            manga_json=manga,
            warnings=warnings,
        )

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id}/generate-image",
        response_model=PanelImageGenerationResponse,
    )
    async def generate_panel_image(
        project_id: str,
        panel_id: str,
        request: Request,
        payload: GenerationJobCreate | None = Body(default=None),
    ) -> PanelImageGenerationResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        manager: JobManager = request.app.state.job_manager
        if find_active_panel_job(manager, project_id, panel_id):
            raise HTTPException(status_code=409, detail="このコマは画像生成中です")
        # 同期レスポンスが欲しい直接生成APIも、ジョブを作って完了待ちする薄いラッパーにする。
        candidate_count = payload.candidate_count if payload else 1
        job = manager.create(project_id, panel_id, candidate_count)
        panel.generation.status = "queued"
        panel.generation.message = "画像生成ジョブを登録しました"
        save_manga_json(request, project_id, manga)
        manager.start(job, run_generation_job(request.app, job))
        await await_job(request.app, job)
        latest = load_project_record(request, project_id)
        return PanelImageGenerationResponse(
            project_id=project_id, panel_id=panel_id, manga_json=parse_manga_json(latest.manga_json)
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
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        manager: JobManager = request.app.state.job_manager
        active = next(
            (
                item
                for item in manager.jobs.values()
                if item.project_id == project_id
                and item.panel_id == panel_id
                and item.status not in TERMINAL_JOB_STATUSES
            ),
            None,
        )
        if active:
            raise HTTPException(status_code=409, detail="このコマは画像生成中です")
        candidate_count = payload.candidate_count if payload else 1
        job = manager.create(project_id, panel_id, candidate_count)
        panel.generation.status = "queued"
        panel.generation.message = "画像生成ジョブを登録しました"
        save_manga_json(request, project_id, manga)
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
        jobs: list[GenerationJob] = []
        for panel in panels:
            if (project_id, panel.panel_id) in active_keys:
                continue
            job = manager.create(project_id, panel.panel_id, options.candidate_count)
            panel.generation.status = "queued"
            panel.generation.message = "一括生成キューへ登録しました"
            jobs.append(job)
        if not jobs:
            raise HTTPException(status_code=409, detail="対象コマはすべて生成中です")
        save_manga_json(request, project_id, manga)
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
    def cancel_generation_job(job_id: str, request: Request) -> GenerationJobResponse:
        manager: JobManager = request.app.state.job_manager
        job = get_job_or_404(request, job_id)
        # ローカルTaskを止める前にprompt_idを控え、ComfyUI側も停止する。
        remote = stop_comfyui_generation(request.app.state.settings, job)
        manager.cancel(job)
        if remote is False:
            # ローカルではキャンセル済みだがリモート停止に失敗した状態を区別して伝える。
            manager.update(
                job,
                status="cancelled",
                message="ローカルではキャンセルしましたが、ComfyUI側の停止に失敗しました",
            )
        mark_panel_job_stopped(request.app, job, "生成をキャンセルしました")
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
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        candidate = next((item for item in panel.image_candidates if item.id == candidate_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail="画像候補が見つかりません")
        apply_candidate_selection(panel, candidate)
        settings = request.app.state.settings
        page_number = find_panel_page_number(manga, panel_id)
        page_asset, warnings = render_project_page(
            project_id, manga, page_number, settings.export_dir
        )
        page = next(item for item in manga.pages if item.page == page_number)
        page.render_status = "done"
        page.rendered_at = now_utc()
        save_manga_json(request, project_id, manga)
        return PanelPageRenderResponse(
            project_id=project_id,
            panel_id=panel_id,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
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
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        settings = request.app.state.settings
        result = await StubImageBackend().generate_panel(project_id, panel, settings.export_dir)
        register_generation_candidate(panel, panel, result)
        save_manga_json(request, project_id, manga)
        return PanelImageGenerationResponse(
            project_id=project_id, panel_id=panel_id, manga_json=manga
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
        manga = parse_manga_json(record.manga_json)
        character = next((item for item in manga.characters if item.id == character_id), None)
        if character is None:
            raise HTTPException(status_code=404, detail="キャラクターが見つかりません")
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "references"
            / stable_asset_name(character_id, "character")
        )
        await save_request_image(request, target)
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)
        character.reference_image_asset = asset_id
        save_manga_json(request, project_id, manga)
        return CharacterReferenceResponse(
            character_id=character_id,
            asset=asset_id,
            manga_json=manga,
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
        manga = parse_manga_json(record.manga_json)
        location = next((item for item in manga.locations if item.id == location_id), None)
        if location is None:
            raise HTTPException(status_code=404, detail="ロケーションが見つかりません")
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "locations"
            / stable_asset_name(location_id, "location")
        )
        await save_request_image(request, target)
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)
        location.reference_image_asset = asset_id
        save_manga_json(request, project_id, manga)
        return ReferenceAssetResponse(target_id=location_id, asset=asset_id, manga_json=manga)

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
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "controls"
            / stable_asset_name(panel_id, "panel").removesuffix(".png")
            / f"{kind}.png"
        )
        await save_request_image(request, target)
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)
        existing = next((item for item in panel.control_references if item.kind == kind), None)
        if existing:
            existing.asset = asset_id
            existing.load_node_id = load_node_id
            target_id = existing.id
        else:
            control = PanelControlReference(
                id=str(uuid.uuid4()), kind=kind, asset=asset_id, load_node_id=load_node_id
            )
            panel.control_references.append(control)
            target_id = control.id
        save_manga_json(request, project_id, manga)
        return ReferenceAssetResponse(target_id=target_id, asset=asset_id, manga_json=manga)

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
        manga = parse_manga_json(record.manga_json)
        page = next((item for item in manga.pages if item.page == page_number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="ページが見つかりません")
        overlay = next((item for item in page.overlay_elements if item.id == overlay_id), None)
        if overlay is None:
            raise HTTPException(status_code=404, detail="overlayが見つかりません")
        suffix = "mask" if asset_kind == "mask" else "image"
        target = (
            request.app.state.settings.export_dir
            / safe_component(project_id, "project")
            / "overlays"
            / stable_asset_name(overlay_id, "overlay", suffix)
        )
        await save_request_image(request, target, preserve_alpha=asset_kind == "asset")
        asset_id = path_to_asset_id(target, request.app.state.settings.export_dir)
        if asset_kind == "mask":
            overlay.mask_asset = asset_id
        else:
            overlay.asset = asset_id
        save_manga_json(request, project_id, manga)
        return ReferenceAssetResponse(target_id=overlay_id, asset=asset_id, manga_json=manga)

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
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
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
        save_manga_json(request, project_id, manga)
        return LayoutSuggestResponse(
            project_id=project_id,
            page=page_number,
            layout_family=page.layout_family,
            manga_json=manga,
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
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        page = next((item for item in manga.pages if item.page == page_number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="ページが見つかりません")
        settings = request.app.state.settings
        page_asset, warnings = render_project_page(
            project_id, manga, page_number, settings.export_dir
        )
        page.render_status = "done"
        page.rendered_at = now_utc()
        save_manga_json(request, project_id, manga)
        issues = preflight_module.preflight_page(manga, page, export_dir=settings.export_dir)
        return PageRenderResponse(
            project_id=project_id,
            page=page_number,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
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
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        page_number = find_panel_page_number(manga, panel_id)
        settings = request.app.state.settings
        page_asset, warnings = render_project_page(
            project_id, manga, page_number, settings.export_dir
        )
        page = next(item for item in manga.pages if item.page == page_number)
        page.render_status = "done"
        page.rendered_at = now_utc()
        save_manga_json(request, project_id, manga)
        return PanelPageRenderResponse(
            project_id=project_id,
            panel_id=panel_id,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
            warnings=warnings,
        )

    @app.post("/api/projects/{project_id}/export/cbz", response_model=ExportResponse)
    def export_project_cbz(project_id: str, request: Request) -> ExportResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        settings = request.app.state.settings
        status = build_production_status(project_id, manga)
        # 重大エラー（台詞のはみ出し等）があればCBZ出力を停止する。
        preflight_errors = [
            issue
            for issue in preflight_module.preflight_project(manga, settings.export_dir)
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
        page_assets, render_warnings = render_project_pages(project_id, manga, settings.export_dir)
        for page in manga.pages:
            page.render_status = "done"
            page.rendered_at = now_utc()
        save_manga_json(request, project_id, manga)
        cbz_path = export_cbz(project_id, manga.title, page_assets, settings.export_dir)
        return ExportResponse(
            project_id=project_id,
            cbz_asset=asset_to_id(cbz_path, settings.export_dir),
            absolute_path=str(cbz_path.resolve()),
            warnings=[blocker for blocker in status.blockers if "採用画像" in blocker]
            + render_warnings,
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
        return build_production_status(project_id, parse_manga_json(record.manga_json))

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
        with request.app.state.SessionLocal() as session:
            if payload.knowledge_work_id:
                try:
                    local_work = knowledge_db.load_local_work(
                        request.app.state.settings.knowledge_dir, payload.knowledge_work_id
                    )
                    knowledge_db.sync_local_work(session, local_work)
                except knowledge_db.LocalKnowledgeError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                work_name = local_work.work_name
                project = session.get(ProjectRecord, project_id)
                if project is None:
                    raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
                manga = parse_manga_json(project.manga_json)
                manga.work_name = work_name
                project.work_name = work_name
                project.manga_json = manga.model_dump_json()
                project.revision += 1
                project.updated_at = now_utc()
                session.commit()
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
    def apply_story_session(session_id: str, request: Request) -> ProjectDetail:
        with request.app.state.SessionLocal() as session:
            record = session.get(StoryGenerationSessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
            project = session.get(ProjectRecord, record.project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            try:
                story_module.apply_session(session, record, project)
            except StoryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            session.refresh(project)
            return to_detail(project, request.app.state.settings.export_dir)

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
    def restore_project_revision(
        project_id: str, revision_id: str, request: Request
    ) -> ProjectDetail:
        with request.app.state.SessionLocal() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            revision = session.get(ProjectRevisionRecord, revision_id)
            if revision is None or revision.project_id != project_id:
                raise HTTPException(status_code=404, detail="リビジョンが見つかりません")
            story_module.restore_revision(session, project, revision)
            session.refresh(project)
            return to_detail(project, request.app.state.settings.export_dir)

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


def stop_comfyui_generation(settings: Settings, job: GenerationJob) -> bool | None:
    """ComfyUI側の生成を停止する。停止不要ならNone、成功でTrue、失敗でFalse。"""
    if settings.image_backend.lower() != "comfyui" or not job.prompt_id:
        return None
    base = settings.comfyui_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as client:
            # 実行中ならinterrupt、キュー待ちならprompt_id指定でqueueから削除する。
            client.post(f"{base}/interrupt")
            client.post(f"{base}/queue", json={"delete": [job.prompt_id]})
        return True
    except Exception:
        return False


async def run_generation_job(app: FastAPI, job: GenerationJob) -> None:
    manager: JobManager = app.state.job_manager
    manager.update(job, message="生成キューで待機中です")
    try:
        async with manager.generation_lock:
            await execute_generation_job(app, job)
    except CancelledError:
        if manager.shutting_down:
            manager.update(
                job, status="queued", progress=0, message="バックエンド再起動後に生成を再開します"
            )
            raise
        mark_panel_job_stopped(app, job, "生成をキャンセルしました")
        if job.status != "cancelled":
            manager.update(job, status="cancelled", message="生成をキャンセルしました")
        raise


async def execute_generation_job(app: FastAPI, job: GenerationJob) -> None:
    manager: JobManager = app.state.job_manager
    try:

        def set_running(panel) -> None:
            panel.generation.status = "running"
            panel.generation.message = "画像候補を生成中です"

        # 最新を読み直して対象パネルだけ更新し、並行編集を踏みつぶさない。
        manga = update_panel_in_latest(app, job.project_id, job.panel_id, set_running)
        panel = find_panel(manga, job.panel_id)
        manager.update(job, status="running", message="画像候補を生成中です")
        backend = build_image_backend(app.state.settings)

        for candidate_index in range(job.candidate_count):
            candidate_id = str(uuid.uuid4())
            generated_panel = prepare_panel_for_generation(manga, panel)
            generated_panel.generation.seed += candidate_index
            target = (
                app.state.settings.export_dir
                / job.project_id
                / "panels"
                / job.panel_id
                / f"{candidate_id}.png"
            )

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
            # キャンセル要求後に生成物が返っても、対象ジョブがキャンセル済みなら保存しない。
            if job.status == "cancelled":
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

            def add_candidate(target_panel, new_candidate=candidate) -> None:
                target_panel.image_candidates.append(new_candidate)
                apply_candidate_selection(target_panel, new_candidate)

            # 候補ごとに最新を読み直してマージし、生成中のユーザー編集を残す。
            update_panel_in_latest(app, job.project_id, job.panel_id, add_candidate)
            job.candidate_ids.append(candidate_id)
            manager.update(
                job,
                progress=round(((candidate_index + 1) / job.candidate_count) * 100),
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
        if manager.shutting_down:
            manager.update(
                job, status="queued", progress=0, message="バックエンド再起動後に生成を再開します"
            )
            raise
        mark_panel_job_stopped(app, job, "生成をキャンセルしました")
        if job.status != "cancelled":
            manager.update(job, status="cancelled", message="生成をキャンセルしました")
        raise
    except Exception as exc:
        mark_panel_job_stopped(app, job, f"画像候補の生成に失敗しました: {exc}", error=True)
        manager.update(
            job, status="error", node=None, message=f"画像候補の生成に失敗しました: {exc}"
        )


def mark_panel_job_stopped(
    app: FastAPI, job: GenerationJob, message: str, error: bool = False
) -> None:
    def mutate(panel) -> None:
        panel.generation.status = "error" if error else "skipped"
        panel.generation.message = message

    try:
        update_panel_in_latest(app, job.project_id, job.panel_id, mutate)
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


def build_production_status(project_id: str, manga: MangaProject) -> ProjectProductionStatus:
    page_statuses: list[PageProductionStatus] = []
    project_blockers: list[str] = []
    adopted_total = 0
    panel_total = 0
    rendered_pages = 0
    for page in manga.pages:
        total = len(page.panels)
        adopted = sum(
            1
            for panel in page.panels
            if panel.selected_candidate_id
            and any(
                candidate.id == panel.selected_candidate_id for candidate in panel.image_candidates
            )
        )
        rendered = page.render_status == "done"
        blockers: list[str] = []
        for panel in page.panels:
            if not panel.selected_candidate_id:
                blockers.append(f"{panel.panel_id}: 採用画像が未選択です")
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
        record.manga_json = normalize_manga_assets(
            manga, request.app.state.settings.export_dir
        ).model_dump_json()
        return record


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
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return panel
    return None


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
        else:
            page.render_status = "pending"
            page.rendered_at = None


def update_panel_in_latest(app: FastAPI, project_id: str, panel_id: str, mutate) -> MangaProject:
    """最新のmanga_jsonを読み直し、対象パネルだけにmutateを適用して保存する。

    生成完了時に開始時点の古いスナップショットを丸ごと保存すると、その間の
    ユーザー編集（他パネルの台詞・別ページの候補選択）を踏みつぶす。読み直して
    対象パネルだけマージすることで、生成とユーザー編集の競合面を最小化する。
    """
    with app.state.SessionLocal() as session:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            raise RuntimeError("プロジェクトが見つかりません")
        manga = parse_manga_json(record.manga_json)
        panel = find_panel_optional(manga, panel_id)
        if panel is None:
            raise RuntimeError("コマが見つかりません")
        mutate(panel)
        normalize_manga_assets(manga, app.state.settings.export_dir)
        record.manga_json = manga.model_dump_json()
        record.revision += 1
        record.updated_at = now_utc()
        session.commit()
    return manga


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


def register_generation_candidate(panel, generated_panel, result) -> None:
    if result.asset_path is None:
        apply_generation_result(panel, result)
        return
    candidate = ImageCandidate(
        id=str(uuid.uuid4()),
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


def save_manga_json(request: Request, project_id: str, manga: MangaProject) -> None:
    normalize_manga_assets(manga, request.app.state.settings.export_dir)
    with request.app.state.SessionLocal() as session:
        writable = session.get(ProjectRecord, project_id)
        if writable is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        writable.manga_json = manga.model_dump_json()
        writable.revision += 1
        writable.updated_at = now_utc()
        session.commit()


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
