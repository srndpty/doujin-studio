from __future__ import annotations

import io
import json
import subprocess
import sys
import uuid
from asyncio import CancelledError
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError
from PIL import Image

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
from . import fonts as font_registry
from . import knowledge as knowledge_db
from . import layout_engine
from . import story as story_module
from .llm import build_llm_client, get_llm_status
from .image_backends import StubImageBackend, build_image_backend, get_comfyui_status
from .jobs import GenerationJob, JobManager, TERMINAL_JOB_STATUSES
from .prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from .renderer import export_cbz, render_project_page, render_project_pages
from .schemas import (
    ExportResponse,
    BatchGenerationJobCreate,
    BatchGenerationJobResponse,
    CharacterReferenceResponse,
    ReferenceAssetResponse,
    ComfyUIStatusResponse,
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
    LocalKnowledgeSyncResponse,
    LocalKnowledgeWorkResponse,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceResponse,
    LayoutSuggestRequest,
    LayoutSuggestResponse,
    LLMStatusResponse,
    MangaProject,
    OpenExportFolderResponse,
    PanelImageGenerationResponse,
    PanelControlReference,
    PanelPageRenderResponse,
    PromptPreviewResponse,
    PageProductionStatus,
    ProjectProductionStatus,
    ProjectCreate,
    ProjectDetail,
    ProjectRevisionResponse,
    ProjectSummary,
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

    @app.get("/api/comfyui/status", response_model=ComfyUIStatusResponse)
    async def comfyui_status(request: Request) -> ComfyUIStatusResponse:
        status = await get_comfyui_status(request.app.state.settings)
        return ComfyUIStatusResponse(**status.__dict__)

    @app.post("/api/projects", response_model=ProjectDetail)
    def create_project(payload: ProjectCreate, request: Request) -> ProjectDetail:
        project_id = str(uuid.uuid4())
        manga = MangaProject(title=payload.title, work_name=payload.work_name, target_pages=payload.target_pages)
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
        return to_detail(record)

    @app.get("/api/projects", response_model=list[ProjectSummary])
    def list_projects(request: Request) -> list[ProjectSummary]:
        with request.app.state.SessionLocal() as session:
            records = session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all()
        return [to_summary(record) for record in records]

    @app.get("/api/projects/{project_id}", response_model=ProjectDetail)
    def get_project(project_id: str, request: Request) -> ProjectDetail:
        record = load_project_record(request, project_id)
        return to_detail(record)

    @app.post("/api/projects/{project_id}/generate-name", response_model=ProjectDetail)
    def generate_name(project_id: str, payload: GenerateNameRequest, request: Request) -> ProjectDetail:
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
            record.updated_at = now_utc()
            session.commit()
            session.refresh(record)
        return to_detail(record)

    @app.put("/api/projects/{project_id}/manga-json", response_model=ProjectDetail)
    def update_manga_json(project_id: str, payload: MangaProject, request: Request) -> ProjectDetail:
        for page in payload.pages:
            page.render_status = "pending"
            page.rendered_at = None
        with request.app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, project_id)
            if record is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            record.title = payload.title
            record.work_name = payload.work_name
            record.manga_json = payload.model_dump_json()
            record.updated_at = now_utc()
            session.commit()
            session.refresh(record)
        return to_detail(record)

    @app.post("/api/projects/{project_id}/render", response_model=RenderResponse)
    async def render_project(
        project_id: str,
        request: Request,
        payload: RenderRequest | None = Body(default=None),
    ) -> RenderResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        settings = request.app.state.settings
        backend = build_image_backend(settings)
        force = payload.force if payload else False

        for page in manga.pages:
            for panel in page.panels:
                if force or not panel.image_asset:
                    prepared = prepare_panel_for_generation(manga, panel)
                    result = await backend.generate_panel(project_id, prepared, settings.export_dir)
                    register_generation_candidate(panel, prepared, result)

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

    @app.post("/api/projects/{project_id}/panels/{panel_id}/generate-image", response_model=PanelImageGenerationResponse)
    async def generate_panel_image(
        project_id: str,
        panel_id: str,
        request: Request,
    ) -> PanelImageGenerationResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        panel = find_panel(manga, panel_id)
        settings = request.app.state.settings
        backend = build_image_backend(settings)
        prepared = prepare_panel_for_generation(manga, panel)
        result = await backend.generate_panel(project_id, prepared, settings.export_dir)
        register_generation_candidate(panel, prepared, result)
        save_manga_json(request, project_id, manga)
        return PanelImageGenerationResponse(project_id=project_id, panel_id=panel_id, manga_json=manga)

    @app.get(
        "/api/projects/{project_id}/panels/{panel_id}/prompt-preview",
        response_model=PromptPreviewResponse,
    )
    def preview_panel_prompt(project_id: str, panel_id: str, request: Request) -> PromptPreviewResponse:
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

    @app.post("/api/projects/{project_id}/generation-jobs", response_model=BatchGenerationJobResponse)
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

    @app.get("/api/projects/{project_id}/generation-jobs", response_model=list[GenerationJobResponse])
    def list_generation_jobs(project_id: str, request: Request) -> list[GenerationJobResponse]:
        load_project_record(request, project_id)
        manager: JobManager = request.app.state.job_manager
        return [to_job_response(job) for job in manager.list_for_project(project_id)]

    @app.post("/api/generation-jobs/{job_id}/cancel", response_model=GenerationJobResponse)
    def cancel_generation_job(job_id: str, request: Request) -> GenerationJobResponse:
        manager: JobManager = request.app.state.job_manager
        job = get_job_or_404(request, job_id)
        manager.cancel(job)
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
        page_asset, warnings = render_project_page(project_id, manga, page_number, settings.export_dir)
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

    @app.post("/api/projects/{project_id}/panels/{panel_id}/use-stub", response_model=PanelImageGenerationResponse)
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
        return PanelImageGenerationResponse(project_id=project_id, panel_id=panel_id, manga_json=manga)

    @app.post(
        "/api/projects/{project_id}/characters/{character_id}/reference-image",
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
        target = request.app.state.settings.export_dir / project_id / "references" / f"{character_id}.png"
        await save_request_image(request, target)
        character.reference_image_asset = str(target)
        save_manga_json(request, project_id, manga)
        return CharacterReferenceResponse(
            character_id=character_id,
            asset=str(target),
            manga_json=manga,
        )

    @app.post(
        "/api/projects/{project_id}/locations/{location_id}/reference-image",
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
        target = request.app.state.settings.export_dir / project_id / "locations" / f"{location_id}.png"
        await save_request_image(request, target)
        location.reference_image_asset = str(target)
        save_manga_json(request, project_id, manga)
        return ReferenceAssetResponse(target_id=location_id, asset=str(target), manga_json=manga)

    @app.post(
        "/api/projects/{project_id}/panels/{panel_id}/controls/{kind}/reference-image",
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
        target = request.app.state.settings.export_dir / project_id / "controls" / panel_id / f"{kind}.png"
        await save_request_image(request, target)
        existing = next((item for item in panel.control_references if item.kind == kind), None)
        if existing:
            existing.asset = str(target)
            existing.load_node_id = load_node_id
            target_id = existing.id
        else:
            control = PanelControlReference(
                id=str(uuid.uuid4()), kind=kind, asset=str(target), load_node_id=load_node_id
            )
            panel.control_references.append(control)
            target_id = control.id
        save_manga_json(request, project_id, manga)
        return ReferenceAssetResponse(target_id=target_id, asset=str(target), manga_json=manga)

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

    @app.post("/api/projects/{project_id}/panels/{panel_id}/render-page", response_model=PanelPageRenderResponse)
    def render_panel_page(project_id: str, panel_id: str, request: Request) -> PanelPageRenderResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        page_number = find_panel_page_number(manga, panel_id)
        settings = request.app.state.settings
        page_asset, warnings = render_project_page(project_id, manga, page_number, settings.export_dir)
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
            warnings=[blocker for blocker in status.blockers if "採用画像" in blocker] + render_warnings,
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
        cbz_path = max(cbz_files, key=lambda path: path.stat().st_mtime) if cbz_files else project_dir
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
    def import_knowledge_sources(payload: KnowledgeImportRequest, request: Request) -> KnowledgeImportResponse:
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
        return KnowledgeImportResponse(sources=sources)

    @app.post("/api/knowledge/documents", response_model=KnowledgeSourceResponse)
    def add_knowledge_document(payload: KnowledgeDocumentRequest, request: Request) -> KnowledgeSourceResponse:
        with request.app.state.SessionLocal() as session:
            record = knowledge_db.import_source(
                session,
                work_name=payload.work_name,
                title=payload.title or "無題ドキュメント",
                doc_type=payload.doc_type,
                usage=payload.usage,
                content=payload.content,
            )
            return to_knowledge_source(record)

    @app.get("/api/knowledge/sources", response_model=list[KnowledgeSourceResponse])
    def list_knowledge_sources(request: Request, work_name: str | None = None) -> list[KnowledgeSourceResponse]:
        with request.app.state.SessionLocal() as session:
            return [to_knowledge_source(record) for record in knowledge_db.list_sources(session, work_name)]

    @app.delete("/api/knowledge/sources/{source_id}")
    def delete_knowledge_source(source_id: str, request: Request) -> dict[str, bool]:
        with request.app.state.SessionLocal() as session:
            if not knowledge_db.delete_source(session, source_id):
                raise HTTPException(status_code=404, detail="知識ソースが見つかりません")
        return {"ok": True}

    @app.post("/api/knowledge/search", response_model=KnowledgeSearchResponse)
    def search_knowledge(payload: KnowledgeSearchRequest, request: Request) -> KnowledgeSearchResponse:
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
    def create_story_session(project_id: str, payload: StorySessionCreate, request: Request) -> StorySessionResponse:
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

    @app.post("/api/story-sessions/{session_id}/stages/{stage}/generate", response_model=StorySessionResponse)
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

    @app.post("/api/story-sessions/{session_id}/stages/{stage}/approve", response_model=StorySessionResponse)
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
            return to_detail(project)

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
                    id=record.id, project_id=record.project_id, label=record.label, created_at=record.created_at
                )
                for record in records
            ]

    @app.post("/api/projects/{project_id}/revisions/{revision_id}/restore", response_model=ProjectDetail)
    def restore_project_revision(project_id: str, revision_id: str, request: Request) -> ProjectDetail:
        with request.app.state.SessionLocal() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            revision = session.get(ProjectRevisionRecord, revision_id)
            if revision is None or revision.project_id != project_id:
                raise HTTPException(status_code=404, detail="リビジョンが見つかりません")
            story_module.restore_revision(session, project, revision)
            session.refresh(project)
            return to_detail(project)

    @app.get("/api/assets/{asset_id:path}")
    def get_asset(asset_id: str, request: Request) -> FileResponse:
        export_root = request.app.state.settings.export_dir.resolve()
        target = (export_root / asset_id).resolve()
        if not str(target).startswith(str(export_root)) or not target.exists():
            raise HTTPException(status_code=404, detail="アセットが見つかりません")
        return FileResponse(target)

    return app


async def run_generation_job(app: FastAPI, job: GenerationJob) -> None:
    manager: JobManager = app.state.job_manager
    manager.update(job, message="生成キューで待機中です")
    try:
        async with manager.generation_lock:
            await execute_generation_job(app, job)
    except CancelledError:
        if manager.shutting_down:
            manager.update(job, status="queued", progress=0, message="バックエンド再起動後に生成を再開します")
            raise
        mark_panel_job_stopped(app, job, "生成をキャンセルしました")
        if job.status != "cancelled":
            manager.update(job, status="cancelled", message="生成をキャンセルしました")
        raise


async def execute_generation_job(app: FastAPI, job: GenerationJob) -> None:
    manager: JobManager = app.state.job_manager
    try:
        manga = load_manga_for_app(app, job.project_id)
        panel = find_panel(manga, job.panel_id)
        panel.generation.status = "running"
        panel.generation.message = "画像候補を生成中です"
        save_manga_json_for_app(app, job.project_id, manga)
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

            async def report_progress(current: int, total: int, node: str | None, message: str) -> None:
                fraction = current / max(total, 1)
                overall = round(((candidate_index + fraction) / job.candidate_count) * 100)
                manager.update(
                    job,
                    progress=max(0, min(overall, 99)),
                    current=current,
                    total=total,
                    node=node,
                    message=f"候補 {candidate_index + 1}/{job.candidate_count}: {message}",
                )

            result = await backend.generate_panel(
                job.project_id,
                generated_panel,
                app.state.settings.export_dir,
                target_path=target,
                progress_callback=report_progress,
            )
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
            panel.image_candidates.append(candidate)
            apply_candidate_selection(panel, candidate)
            save_manga_json_for_app(app, job.project_id, manga)
            job.candidate_ids.append(candidate_id)
            manager.update(
                job,
                progress=round(((candidate_index + 1) / job.candidate_count) * 100),
                message=f"候補 {candidate_index + 1}/{job.candidate_count} を保存しました",
            )

        manager.update(job, status="done", progress=100, node=None, message="画像候補の生成が完了しました")
    except CancelledError:
        if manager.shutting_down:
            manager.update(job, status="queued", progress=0, message="バックエンド再起動後に生成を再開します")
            raise
        mark_panel_job_stopped(app, job, "生成をキャンセルしました")
        if job.status != "cancelled":
            manager.update(job, status="cancelled", message="生成をキャンセルしました")
        raise
    except Exception as exc:
        mark_panel_job_stopped(app, job, f"画像候補の生成に失敗しました: {exc}", error=True)
        manager.update(job, status="error", node=None, message=f"画像候補の生成に失敗しました: {exc}")


def load_manga_for_app(app: FastAPI, project_id: str) -> MangaProject:
    with app.state.SessionLocal() as session:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            raise RuntimeError("プロジェクトが見つかりません")
        return parse_manga_json(record.manga_json)


def save_manga_json_for_app(app: FastAPI, project_id: str, manga: MangaProject) -> None:
    with app.state.SessionLocal() as session:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            raise RuntimeError("プロジェクトが見つかりません")
        record.manga_json = manga.model_dump_json()
        record.updated_at = now_utc()
        session.commit()


def mark_panel_job_stopped(app: FastAPI, job: GenerationJob, message: str, error: bool = False) -> None:
    try:
        manga = load_manga_for_app(app, job.project_id)
        panel = find_panel(manga, job.panel_id)
        panel.generation.status = "error" if error else "skipped"
        panel.generation.message = message
        save_manga_json_for_app(app, job.project_id, manga)
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
            and any(candidate.id == panel.selected_candidate_id for candidate in panel.image_candidates)
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
        return record


async def save_request_image(request: Request, target: Path) -> None:
    content = await request.body()
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="参照画像は20MB以下にしてください")
    try:
        with Image.open(io.BytesIO(content)) as source:
            image = source.convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=422, detail="参照画像を読み込めません") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")


def parse_manga_json(raw: str) -> MangaProject:
    try:
        return MangaProject.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"Manga JSONが不正です: {exc}") from exc


def find_panel(manga: MangaProject, panel_id: str):
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return panel
    raise HTTPException(status_code=404, detail="コマが見つかりません")


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
    with request.app.state.SessionLocal() as session:
        writable = session.get(ProjectRecord, project_id)
        if writable is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        writable.manga_json = manga.model_dump_json()
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
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_detail(record: ProjectRecord) -> ProjectDetail:
    return ProjectDetail(**to_summary(record).model_dump(), manga_json=parse_manga_json(record.manga_json))


def asset_to_id(path: Path, export_dir: Path) -> str:
    return path.resolve().relative_to(export_dir.resolve()).as_posix()


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
