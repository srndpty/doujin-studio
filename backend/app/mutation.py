"""ProjectRecordの更新を一箇所へ集約するリポジトリ/サービス。

全更新APIをCAS(UPDATE ... WHERE id AND revision)経由へ寄せ、「古い全文での無条件
上書きで生成結果や他編集を巻き戻す」書き込み競合を防ぐ。docs/refactoring-plan.mdの
ProjectRepository → ProjectMutationService の最初の抽出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, TypeVar

from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.orm import sessionmaker

from .assets import normalize_manga_assets
from .database import ProjectRecord, now_utc
from .schemas import MangaProject, Page

T = TypeVar("T")


class ProjectNotFoundError(Exception):
    pass


class ProjectConflictError(Exception):
    pass


class InvalidProjectJsonError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def mark_page_dirty(page: Page) -> None:
    """描画入力が変わったページを未レンダリング扱いへ戻す共通処理。

    採用画像の変更・overlay画像/maskの差し替え・レイアウト変更などで使う。
    手続き的に各APIが個別にpendingへ戻すより、共通化した方が漏れに強い。
    """
    page.render_status = "pending"
    page.rendered_at = None


def parse_manga(raw: str) -> MangaProject:
    try:
        return MangaProject.model_validate(json.loads(raw))
    except Exception as exc:  # JSON/検証どちらの失敗も保存不能として扱う
        raise InvalidProjectJsonError(f"Manga JSONが不正です: {exc}") from exc


class ProjectMutationService:
    """最新manga_jsonを読み直し、mutateを適用してCAS保存するサービス。

    - expected_revision指定（ユーザー起点）: 読み取り時に不一致なら即409、CAS失敗も409。
    - expected_revision=None（バックグラウンド/暗黙）: 競合時は読み直して再適用するリトライ。
    title/work_nameはmanga本体と常に同期する。
    """

    def __init__(self, session_factory: sessionmaker, export_dir: Path) -> None:
        self.session_factory = session_factory
        self.export_dir = export_dir

    def mutate(
        self,
        project_id: str,
        mutate: Callable[[MangaProject], T],
        *,
        expected_revision: int | None = None,
        attempts: int = 5,
    ) -> tuple[T, MangaProject, int]:
        for _ in range(attempts):
            with self.session_factory() as session:
                record = session.get(ProjectRecord, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                if expected_revision is not None and record.revision != expected_revision:
                    raise ProjectConflictError()
                base = record.revision
                manga = parse_manga(record.manga_json)
                result = mutate(manga)
                normalize_manga_assets(manga, self.export_dir)
                outcome = session.execute(
                    sqlalchemy_update(ProjectRecord)
                    .where(ProjectRecord.id == project_id, ProjectRecord.revision == base)
                    .values(
                        title=manga.title,
                        work_name=manga.work_name,
                        manga_json=manga.model_dump_json(),
                        revision=ProjectRecord.revision + 1,
                        updated_at=now_utc(),
                    )
                )
                if outcome.rowcount == 1:
                    session.commit()
                    return result, manga, base + 1
                session.rollback()
                # ユーザー起点は同値読みの並行commitも競合確定。暗黙更新は読み直して再試行。
                if expected_revision is not None:
                    raise ProjectConflictError()
        raise ProjectConflictError()
