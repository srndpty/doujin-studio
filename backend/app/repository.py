"""ProjectRecordのDBアクセスを一箇所へ集約するリポジトリ層。

ServiceがSQLAlchemy Sessionとトランザクション境界を管理し、Repositoryは
そのSession上での読み取り・CAS更新・revision履歴・epoch終端化など「DB操作の語彙」
だけを担う。CAS(UPDATE ... WHERE id AND revision)のSQLをServiceから分離する。
docs/refactoring-plan.md の ProjectRepository 抽出。
"""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .database import GenerationJobRecord, ProjectRecord, ProjectRevisionRecord, now_utc
from .schemas import MangaProject

_INTERRUPTED_BY_RESTRUCTURE = "作品構成の更新により前の生成を中断しました"


class ProjectRepository:
    """ProjectRecord/関連レコードへのDBアクセス。Sessionは呼び出し側が管理する。"""

    def get(self, session: Session, project_id: str) -> ProjectRecord | None:
        return session.get(ProjectRecord, project_id)

    def list_ordered(self, session: Session) -> list[ProjectRecord]:
        return session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all()

    def add(self, session: Session, record: ProjectRecord) -> None:
        session.add(record)

    def cas_set_manga(
        self,
        session: Session,
        project_id: str,
        base_revision: int,
        manga: MangaProject,
        *,
        require_epoch: int | None = None,
        increment_epoch: bool = False,
    ) -> int:
        """revision一致を条件にmanga_jsonを置換し、影響行数を返す(1で成功)。

        require_epoch指定時はepoch一致もWHERE条件へ加える。increment_epochで世代を進める。
        """
        where = [
            ProjectRecord.id == project_id,
            ProjectRecord.revision == base_revision,
        ]
        if require_epoch is not None:
            where.append(ProjectRecord.generation_epoch == require_epoch)
        values: dict = {
            "title": manga.title,
            "work_name": manga.work_name,
            "manga_json": manga.model_dump_json(),
            "revision": ProjectRecord.revision + 1,
            "updated_at": now_utc(),
        }
        if increment_epoch:
            values["generation_epoch"] = ProjectRecord.generation_epoch + 1
        outcome = session.execute(sqlalchemy_update(ProjectRecord).where(*where).values(**values))
        return cast(CursorResult, outcome).rowcount

    def cancel_jobs_before_epoch(self, session: Session, project_id: str, new_epoch: int) -> None:
        """旧世代の進行中ジョブを終端化する（構成全置換に伴う停止）。"""
        session.execute(
            sqlalchemy_update(GenerationJobRecord)
            .where(
                GenerationJobRecord.project_id == project_id,
                GenerationJobRecord.epoch < new_epoch,
                GenerationJobRecord.status.in_(["queued", "running"]),
            )
            .values(
                status="cancelled",
                prompt_id=None,
                message=_INTERRUPTED_BY_RESTRUCTURE,
                updated_at=now_utc(),
            )
        )

    def cancel_active_jobs_other_epoch(
        self, session: Session, project_id: str, panel_ids: list[str], keep_epoch: int
    ) -> None:
        """対象panelの、保持世代と異なる進行中ジョブを終端化して部分一意indexを解放する。"""
        session.execute(
            sqlalchemy_update(GenerationJobRecord)
            .where(
                GenerationJobRecord.project_id == project_id,
                GenerationJobRecord.panel_id.in_(panel_ids),
                GenerationJobRecord.status.in_(["queued", "running"]),
                GenerationJobRecord.epoch != keep_epoch,
            )
            .values(
                status="cancelled",
                message=_INTERRUPTED_BY_RESTRUCTURE,
                updated_at=now_utc(),
            )
        )

    def active_panel_ids(
        self, session: Session, project_id: str, panel_ids: list[str], epoch: int
    ) -> set[str]:
        """対象panelのうち、同世代の進行中ジョブが存在するpanel_id集合を返す。"""
        return {
            panel_id
            for (panel_id,) in session.query(GenerationJobRecord.panel_id)
            .filter(
                GenerationJobRecord.project_id == project_id,
                GenerationJobRecord.panel_id.in_(panel_ids),
                GenerationJobRecord.status.in_(["queued", "running"]),
                GenerationJobRecord.epoch == epoch,
            )
            .all()
        }

    def add_revision_history(
        self, session: Session, project_id: str, label: str, manga_json: str
    ) -> None:
        session.add(
            ProjectRevisionRecord(
                id=str(uuid.uuid4()),
                project_id=project_id,
                label=label,
                manga_json=manga_json,
                created_at=now_utc(),
            )
        )

    def add_generation_job(self, session: Session, record: GenerationJobRecord) -> None:
        session.add(record)
