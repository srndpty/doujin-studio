"""プロジェクト全体描画・CBZ出力のapplication service。

Routerに残っていた「epoch固定・生成job起動/待機・整合性確認・不変PNG/CBZ公開・確定・
cleanup」のworkflowをここへ集約する。RouterはHTTP境界(decode→service→DTO)だけにし、
本サービスはHTTPExceptionに依存せずdomain例外を送出する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from .assets import normalize_manga_assets
from .config import Settings
from .generation_service import GenerationService, ensure_sync_generation_succeeded
from .mutation import (
    ProjectMutationService,
    ProjectNotFoundError,
    ProjectRevisionConflictError,
    ProjectSnapshot,
    parse_manga,
)
from .preflight import preflight_project
from .rendering import (
    RenderingService,
    build_production_status,
    export_confirmed_cbz,
    find_inconsistent_selected_panel,
    migrate_legacy_render_state,
    render_snapshot_pages,
)
from .repository import ProjectRepository
from .schemas import MangaProject


class RenderEpochChangedError(Exception):
    """描画中に作品構成(epoch)が変わった。"""


class RenderSelectionInconsistentError(Exception):
    """全体描画前に採用画像が欠損/不整合なpanelがある。"""

    def __init__(self, panel_id: str) -> None:
        super().__init__(panel_id)
        self.panel_id = panel_id


class CbzSelectionInconsistentError(Exception):
    """CBZ出力前に採用画像が欠損/不整合なpanelがある。"""

    def __init__(self, panel_id: str) -> None:
        super().__init__(panel_id)
        self.panel_id = panel_id


class CbzPreflightError(Exception):
    """CBZ出力前のプリフライトで重大エラーがある。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class ProjectRenderResult:
    page_assets: list[Path]
    project: ProjectSnapshot
    warnings: list[str]


@dataclass(frozen=True)
class CbzExportResult:
    cbz_path: Path
    project: ProjectSnapshot
    warnings: list[str]


@dataclass(frozen=True)
class _LoadedProject:
    manga: MangaProject
    revision: int
    generation_epoch: int


class ProjectRenderService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        repository: ProjectRepository,
        mutation: ProjectMutationService,
        generation: GenerationService,
        rendering: RenderingService,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.repository = repository
        self.mutation = mutation
        self.generation = generation
        self.rendering = rendering

    def _load(self, project_id: str) -> _LoadedProject:
        """最新manga・revision・epochを読み、旧形式render状態を安全側へ移行する。"""
        export_dir = self.settings.export_dir
        with self.session_factory() as session:
            record = self.repository.get(session, project_id)
            if record is None:
                raise ProjectNotFoundError()
            session.expunge(record)
        manga = parse_manga(record.manga_json)
        if migrate_legacy_render_state(manga, export_dir):
            self.mutation.mutate_local(
                project_id, lambda latest: migrate_legacy_render_state(latest, export_dir)
            )
            with self.session_factory() as session:
                record = self.repository.get(session, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                session.expunge(record)
            manga = parse_manga(record.manga_json)
        normalize_manga_assets(manga, export_dir)
        return _LoadedProject(manga, record.revision, record.generation_epoch)

    async def render_project(
        self, project_id: str, *, force: bool, expected_revision: int
    ) -> ProjectRenderResult:
        loaded = self._load(project_id)
        if loaded.revision != expected_revision:
            raise ProjectRevisionConflictError(project_id, expected_revision)
        started_epoch = loaded.generation_epoch

        def ensure_same_epoch() -> None:
            # /render開始時の世代を固定する。途中でネーム再生成・ストーリー適用・全文構造変更が
            # 起きたら、古い/render要求が新作品へ生成・確定し続けないよう止める。
            if self.mutation.current_epoch(project_id) != started_epoch:
                raise RenderEpochChangedError()

        # ComfyUI呼び出しはワーカーに一本化する。ここでは生成ジョブを積んで完了を待つ。
        for page in loaded.manga.pages:
            for panel in page.panels:
                if not (force or not panel.image_asset):
                    continue
                ensure_same_epoch()
                job = self.generation.find_active_panel_job(project_id, panel.panel_id)
                if job is None:
                    job = self.generation.start(
                        project_id,
                        [panel.panel_id],
                        1,
                        "全体生成ジョブを登録しました",
                        expected_epoch=started_epoch,
                    )[0]
                ensure_sync_generation_succeeded(await self.generation.await_completion(job))
                ensure_same_epoch()

        ensure_same_epoch()
        latest = self._load(project_id)
        # 採用candidateとassetが不整合なpanelがあれば、プレースホルダを完成画像として確定しない。
        inconsistent = find_inconsistent_selected_panel(latest.manga, self.settings.export_dir)
        if inconsistent is not None:
            raise RenderSelectionInconsistentError(inconsistent.panel_id)
        # publish開始からcommitまでを一つのtry/exceptで囲い、途中失敗でも公開済みPNGを回収する。
        published_by_request: dict[Path, bool] = {}
        try:
            page_assets, warnings = render_snapshot_pages(
                project_id,
                latest.manga,
                self.settings.export_dir,
                latest.revision,
                ownership=published_by_request,
            )
            committed = self.rendering.commit_rendered_pages(
                project_id, latest.manga, page_assets, expected_epoch=started_epoch
            )
        except Exception:
            self.rendering.cleanup_published_assets(project_id, published_by_request)
            raise
        return ProjectRenderResult(page_assets=page_assets, project=committed, warnings=warnings)

    def export_cbz(self, project_id: str, *, expected_revision: int) -> CbzExportResult:
        loaded = self._load(project_id)
        if loaded.revision != expected_revision:
            raise ProjectRevisionConflictError(project_id, expected_revision)
        manga = loaded.manga
        preflight_errors = [
            issue
            for issue in preflight_project(manga, self.settings.export_dir)
            if issue.level == "error"
        ]
        if preflight_errors:
            raise CbzPreflightError(
                "プリフライトで重大エラーが見つかりました: "
                + "; ".join(
                    f"{issue.page}ページ {issue.message}" for issue in preflight_errors[:10]
                )
            )
        inconsistent = find_inconsistent_selected_panel(manga, self.settings.export_dir)
        if inconsistent is not None:
            raise CbzSelectionInconsistentError(inconsistent.panel_id)
        status = build_production_status(project_id, manga, self.settings.export_dir)
        blockers = [blocker for blocker in status.blockers if "採用画像" in blocker]
        # CBZ完成までDBをdoneへ進めない。PNG公開後・CBZ生成中の例外でも公開assetを回収する。
        published_by_request: dict[Path, bool] = {}
        try:
            page_assets, render_warnings = render_snapshot_pages(
                project_id,
                manga,
                self.settings.export_dir,
                loaded.revision,
                ownership=published_by_request,
            )
            cbz_path = export_confirmed_cbz(
                project_id,
                manga.title,
                page_assets,
                self.settings.export_dir,
                loaded.revision,
                ownership=published_by_request,
            )
            committed = self.rendering.commit_rendered_pages(
                project_id,
                manga,
                page_assets,
                expected_revision=loaded.revision,
                expected_epoch=loaded.generation_epoch,
            )
        except Exception:
            self.rendering.cleanup_published_assets(project_id, published_by_request)
            raise
        return CbzExportResult(
            cbz_path=cbz_path, project=committed, warnings=blockers + render_warnings
        )
