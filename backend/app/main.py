"""FastAPIアプリケーションの構築と依存配線。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings
from .database import create_session_factory
from .generation_service import GenerationRuntime, GenerationService
from .jobs import JobManager
from .mutation import ProjectMutationService
from .project_render_service import ProjectRenderService
from .rendering import RenderingService
from .repository import ProjectRepository
from .routers import generation, knowledge, projects, story, system
from .routers.common import register_exception_handlers
from .routers.projects import sweep_deletion_tombstones

logger = logging.getLogger(__name__)
SWEEP_SHUTDOWN_TIMEOUT_SECONDS = 2.0


async def wait_for_sweep_shutdown(
    sweep_task: asyncio.Task[None], timeout_seconds: float = SWEEP_SHUTDOWN_TIMEOUT_SECONDS
) -> None:
    """sweepの完了を短時間待つ。to_thread実処理はcancel不能なのでshieldして待つ。"""
    try:
        await asyncio.wait_for(asyncio.shield(sweep_task), timeout=timeout_seconds)
    except TimeoutError:
        logger.warning(
            "削除残骸のsweepは終了待機時間を超えました。バックグラウンド削除が継続する可能性があります"
        )


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
        app.state.rendering = RenderingService(
            app.state.SessionLocal,
            app_settings.export_dir,
            app.state.mutation,
            app.state.repository,
        )
        app.state.job_manager = JobManager(app.state.SessionLocal)
        app.state.generation = GenerationService(
            app.state.SessionLocal,
            app_settings.export_dir,
            GenerationRuntime(
                settings=app_settings,
                jobs=app.state.job_manager,
                mutation=app.state.mutation,
                rendering=app.state.rendering,
                repository=app.state.repository,
            ),
        )
        app.state.project_render = ProjectRenderService(
            app_settings,
            app.state.SessionLocal,
            app.state.repository,
            app.state.mutation,
            app.state.generation,
            app.state.rendering,
        )
        to_start, interrupted = app.state.job_manager.restore_pending()
        for job in interrupted:
            app.state.generation.mark_panel_job_stopped(
                job,
                "バックエンド再起動により中断されました。必要なら再実行してください",
                error=True,
            )
        for job in to_start:
            app.state.job_manager.start(job, app.state.generation.run(job))

        # 前回の削除で消しきれず残った成果物の再回収は、遅いストレージでも起動を
        # 止めないよう別スレッドのバックグラウンドタスクで行う。
        async def _background_sweep() -> None:
            try:
                await asyncio.to_thread(sweep_deletion_tombstones, app_settings.export_dir)
            except Exception:
                logger.exception("削除残骸のsweepに失敗しました")

        sweep_task = asyncio.create_task(_background_sweep())
        try:
            yield
        finally:
            await wait_for_sweep_shutdown(sweep_task)
            await app.state.job_manager.shutdown()

    app = FastAPI(title="Local Doujin Studio", lifespan=lifespan)
    register_exception_handlers(app)
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


# uvicorn/ASGIの標準エントリポイント（backend.app.main:app）。
# 構築・Router登録のみで、DB等の副作用はlifespanで起動時に行う。
app = create_app()
