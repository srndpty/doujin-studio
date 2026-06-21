"""画像生成ジョブ登録のトランザクション境界。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .assets import normalize_manga_assets
from .database import GenerationJobRecord, ProjectRecord, now_utc
from .jobs import GenerationJob
from .mutation import EpochMismatchError, ProjectConflictError, ProjectNotFoundError, parse_manga


class PanelNotFoundError(Exception):
    pass


class ActiveJobConflictError(Exception):
    pass


class GenerationService:
    def __init__(self, session_factory: sessionmaker, export_dir: Path) -> None:
        self.session_factory = session_factory
        self.export_dir = export_dir

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
                record = session.get(ProjectRecord, project_id)
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
                active_db = {
                    panel_id
                    for (panel_id,) in session.query(GenerationJobRecord.panel_id)
                    .filter(
                        GenerationJobRecord.project_id == project_id,
                        GenerationJobRecord.panel_id.in_(panel_ids),
                        GenerationJobRecord.status.in_(["queued", "running"]),
                    )
                    .all()
                }
                active_panels = active_db | {
                    panel_id
                    for panel_id in panel_ids
                    if panels[panel_id].generation.status in {"queued", "running"}
                }
                if active_panels and not skip_active:
                    raise ActiveJobConflictError()
                panel_ids = [panel_id for panel_id in panel_ids if panel_id not in active_panels]
                if not panel_ids:
                    raise ActiveJobConflictError()
                jobs: list[GenerationJob] = []
                for panel_id in panel_ids:
                    panel = panels[panel_id]
                    panel.generation.status = "queued"
                    panel.generation.message = message
                    jobs.append(
                        GenerationJob(
                            project_id=project_id,
                            panel_id=panel_id,
                            candidate_count=candidate_count,
                            epoch=record.generation_epoch,
                            status="queued",
                            message=message,
                        )
                    )
                normalize_manga_assets(manga, self.export_dir)
                outcome = session.execute(
                    sqlalchemy_update(ProjectRecord)
                    .where(
                        ProjectRecord.id == project_id,
                        ProjectRecord.revision == base_revision,
                        ProjectRecord.generation_epoch == required_epoch,
                    )
                    .values(
                        manga_json=manga.model_dump_json(),
                        revision=ProjectRecord.revision + 1,
                        updated_at=now_utc(),
                    )
                )
                if outcome.rowcount != 1:
                    session.rollback()
                    continue
                for job in jobs:
                    session.add(
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
                        )
                    )
                try:
                    session.commit()
                    return jobs
                except IntegrityError as exc:
                    session.rollback()
                    raise ActiveJobConflictError() from exc
        raise ProjectConflictError()
