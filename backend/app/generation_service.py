"""画像生成ジョブ登録のトランザクション境界。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .assets import normalize_manga_assets
from .database import GenerationJobRecord
from .jobs import GenerationJob
from .mutation import EpochMismatchError, ProjectConflictError, ProjectNotFoundError, parse_manga
from .repository import ProjectRepository


class PanelNotFoundError(Exception):
    pass


class ActiveJobConflictError(Exception):
    pass


class GenerationService:
    def __init__(
        self,
        session_factory: sessionmaker,
        export_dir: Path,
        repository: ProjectRepository | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.export_dir = export_dir
        self.repository = repository or ProjectRepository()

    def enqueue(
        self,
        project_id: str,
        panel_ids: list[str],
        candidate_count: int,
        message: str,
        *,
        attempts: int = 5,
        skip_active: bool = False,
        expected_epoch: int | None = None,
    ) -> list[GenerationJob]:
        """panelのqueued化・job追加・revision更新を同一CASトランザクションで確定する。

        expected_epoch指定時は、呼び出し元が固定した世代と一致する場合のみ登録する。
        長時間の/renderが構成全置換をまたいで新作品へジョブを積むのを防ぐ。
        """
        # expected_epoch指定なら最初から固定。未指定なら初回読み取りで固定する。
        required_epoch: int | None = expected_epoch
        for _ in range(attempts):
            with self.session_factory() as session:
                record = self.repository.get(session, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                base_revision = record.revision
                if required_epoch is None:
                    required_epoch = record.generation_epoch
                elif record.generation_epoch != required_epoch:
                    raise EpochMismatchError()
                manga = parse_manga(record.manga_json)
                panels = {panel.panel_id: panel for page in manga.pages for panel in page.panels}
                if any(panel_id not in panels for panel_id in panel_ids):
                    raise PanelNotFoundError()
                # 旧epochのactive履歴は現在世代の登録を妨げない。候補保存はepoch CASで
                # 拒否されるため、DB上もcancelledへ終端化して部分一意indexを解放する。
                self.repository.cancel_active_jobs_other_epoch(
                    session, project_id, panel_ids, required_epoch
                )
                active_db = self.repository.active_panel_ids(
                    session, project_id, panel_ids, required_epoch
                )
                # JSON表示だけactiveで同epochのDB jobが無ければ孤立状態として自己修復する。
                for panel_id in panel_ids:
                    panel = panels[panel_id]
                    if (
                        panel.generation.status in {"queued", "running"}
                        and panel_id not in active_db
                    ):
                        panel.generation.status = "pending"
                        panel.generation.prompt_id = None
                        panel.generation.active_job_id = None
                        panel.generation.message = "対応する生成ジョブがないため状態を復旧しました"
                active_panels = active_db
                if active_panels and not skip_active:
                    raise ActiveJobConflictError()
                panel_ids = [panel_id for panel_id in panel_ids if panel_id not in active_panels]
                if not panel_ids:
                    raise ActiveJobConflictError()
                jobs: list[GenerationJob] = []
                for panel_id in panel_ids:
                    panel = panels[panel_id]
                    job = GenerationJob(
                        project_id=project_id,
                        panel_id=panel_id,
                        candidate_count=candidate_count,
                        epoch=record.generation_epoch,
                        status="queued",
                        message=message,
                    )
                    panel.generation.status = "queued"
                    panel.generation.active_job_id = job.id
                    panel.generation.message = message
                    jobs.append(job)
                normalize_manga_assets(manga, self.export_dir)
                if (
                    self.repository.cas_set_manga(
                        session,
                        project_id,
                        base_revision,
                        manga,
                        require_epoch=required_epoch,
                    )
                    != 1
                ):
                    session.rollback()
                    continue
                for job in jobs:
                    self.repository.add_generation_job(
                        session,
                        GenerationJobRecord(
                            id=job.id,
                            project_id=project_id,
                            panel_id=job.panel_id,
                            candidate_count=job.candidate_count,
                            epoch=job.epoch,
                            status=job.status,
                            message=job.message,
                            candidate_ids_json="[]",
                            created_at=job.created_at,
                            updated_at=job.updated_at,
                        ),
                    )
                try:
                    session.commit()
                    return jobs
                except IntegrityError as exc:
                    session.rollback()
                    raise ActiveJobConflictError() from exc
        raise ProjectConflictError()
