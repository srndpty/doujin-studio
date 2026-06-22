"""FastAPIアプリケーションの構築と依存配線。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings
from .database import create_session_factory
from .generation_service import GenerationService, mark_panel_job_stopped, run_generation_job
from .jobs import JobManager
from .mutation import ProjectMutationService
from .rendering import RenderingService
from .repository import ProjectRepository
from .routers import generation, knowledge, projects, story, system


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_settings.export_dir.mkdir(parents=True, exist_ok=True)
        app_settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        Path("data").mkdir(parents=True, exist_ok=True)
        app.state.settings = app_settings
        app.state.SessionLocal = create_session_factory(app_settings.database_url)
        app.state.repository = ProjectRepository()
        app.state.mutation = ProjectMutationService(
            app.state.SessionLocal, app_settings.export_dir, app.state.repository
        )
        app.state.generation = GenerationService(
            app.state.SessionLocal, app_settings.export_dir, app.state.repository
        )
        app.state.rendering = RenderingService(
            app.state.SessionLocal,
            app_settings.export_dir,
            app.state.mutation,
            app.state.repository,
        )
        app.state.job_manager = JobManager(app.state.SessionLocal)
        to_start, interrupted = app.state.job_manager.restore_pending()
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
    app.include_router(system.router)
    app.include_router(projects.router)
    app.include_router(generation.router)
    app.include_router(knowledge.router)
    app.include_router(story.router)
    return app
