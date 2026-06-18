from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError

from .config import Settings
from .database import ProjectRecord, create_session_factory, now_utc
from .generator import generate_four_page_name
from .image_backends import StubImageBackend, build_image_backend, get_comfyui_status
from .renderer import export_cbz, render_project_page, render_project_pages
from .schemas import (
    ExportResponse,
    ComfyUIStatusResponse,
    GenerateNameRequest,
    MangaProject,
    PanelImageGenerationResponse,
    PanelPageRenderResponse,
    ProjectCreate,
    ProjectDetail,
    ProjectSummary,
    RenderRequest,
    RenderResponse,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_settings.export_dir.mkdir(parents=True, exist_ok=True)
        Path("data").mkdir(parents=True, exist_ok=True)
        app.state.settings = app_settings
        app.state.SessionLocal = create_session_factory(app_settings.database_url)
        yield

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

    @app.get("/api/comfyui/status", response_model=ComfyUIStatusResponse)
    async def comfyui_status(request: Request) -> ComfyUIStatusResponse:
        status = await get_comfyui_status(request.app.state.settings)
        return ComfyUIStatusResponse(**status.__dict__)

    @app.post("/api/projects", response_model=ProjectDetail)
    def create_project(payload: ProjectCreate, request: Request) -> ProjectDetail:
        project_id = str(uuid.uuid4())
        manga = MangaProject(title=payload.title, work_name=payload.work_name, target_pages=4)
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
                    apply_generation_result(panel, await backend.generate_panel(project_id, panel, settings.export_dir))

        page_assets = render_project_pages(project_id, manga, settings.export_dir)
        save_manga_json(request, project_id, manga)
        return RenderResponse(
            project_id=project_id,
            page_assets=[asset_to_id(path, settings.export_dir) for path in page_assets],
            manga_json=manga,
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
        apply_generation_result(panel, await backend.generate_panel(project_id, panel, settings.export_dir))
        save_manga_json(request, project_id, manga)
        return PanelImageGenerationResponse(project_id=project_id, panel_id=panel_id, manga_json=manga)

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
        apply_generation_result(panel, await StubImageBackend().generate_panel(project_id, panel, settings.export_dir))
        save_manga_json(request, project_id, manga)
        return PanelImageGenerationResponse(project_id=project_id, panel_id=panel_id, manga_json=manga)

    @app.post("/api/projects/{project_id}/panels/{panel_id}/render-page", response_model=PanelPageRenderResponse)
    def render_panel_page(project_id: str, panel_id: str, request: Request) -> PanelPageRenderResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        page_number = find_panel_page_number(manga, panel_id)
        settings = request.app.state.settings
        page_asset = render_project_page(project_id, manga, page_number, settings.export_dir)
        return PanelPageRenderResponse(
            project_id=project_id,
            panel_id=panel_id,
            page_asset=asset_to_id(page_asset, settings.export_dir),
            manga_json=manga,
        )

    @app.post("/api/projects/{project_id}/export/cbz", response_model=ExportResponse)
    def export_project_cbz(project_id: str, request: Request) -> ExportResponse:
        record = load_project_record(request, project_id)
        manga = parse_manga_json(record.manga_json)
        settings = request.app.state.settings
        page_assets = render_project_pages(project_id, manga, settings.export_dir)
        cbz_path = export_cbz(project_id, page_assets, settings.export_dir)
        return ExportResponse(project_id=project_id, cbz_asset=asset_to_id(cbz_path, settings.export_dir))

    @app.get("/api/assets/{asset_id:path}")
    def get_asset(asset_id: str, request: Request) -> FileResponse:
        export_root = request.app.state.settings.export_dir.resolve()
        target = (export_root / asset_id).resolve()
        if not str(target).startswith(str(export_root)) or not target.exists():
            raise HTTPException(status_code=404, detail="アセットが見つかりません")
        return FileResponse(target)

    return app


def load_project_record(request: Request, project_id: str) -> ProjectRecord:
    with request.app.state.SessionLocal() as session:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        session.expunge(record)
        return record


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


def save_manga_json(request: Request, project_id: str, manga: MangaProject) -> None:
    with request.app.state.SessionLocal() as session:
        writable = session.get(ProjectRecord, project_id)
        if writable is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        writable.manga_json = manga.model_dump_json()
        writable.updated_at = now_utc()
        session.commit()


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


app = create_app()
