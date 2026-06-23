"""ProjectRecordの更新を一箇所へ集約するリポジトリ/サービス。

全更新APIをCAS(UPDATE ... WHERE id AND revision)経由へ寄せ、「古い全文での無条件
上書きで生成結果や他編集を巻き戻す」書き込み競合を防ぐ。docs/refactoring-plan.mdの
ProjectRepository → ProjectMutationService の最初の抽出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from .assets import normalize_manga_assets
from .repository import ProjectRepository
from .schemas import MangaProject, Page, Panel

if TYPE_CHECKING:
    from .database import ProjectRecord

T = TypeVar("T")


class ProjectNotFoundError(Exception):
    pass


class ProjectConflictError(Exception):
    def __init__(self, project_id: str | None = None) -> None:
        super().__init__(project_id)
        self.project_id = project_id


class ProjectRevisionConflictError(ProjectConflictError):
    """ユーザーが送った期待revisionと最新revisionが一致しない。"""

    def __init__(self, project_id: str, expected_revision: int) -> None:
        super().__init__(project_id)
        self.expected_revision = expected_revision


class ProjectReplaceConflictError(ProjectRevisionConflictError):
    """全文置換(replace / replace_with_history)でのrevision競合。

    汎用のProjectConflictErrorとは別メッセージを維持するためのサブクラス。
    """


class RenderCommitConflictError(ProjectConflictError):
    """描画結果のdone確定時のrevision競合（CBZ/ページrender確定）。"""


class PanelNotFoundError(Exception):
    """対象panel_idがmanga内に存在しない。"""


class WorkerScopeViolationError(Exception):
    """worker mutationが対象panel以外のpanelを書き換えた（API境界違反）。"""


class InvalidProjectJsonError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class EpochMismatchError(Exception):
    """ジョブ開始時とproject世代が変わった（ページ構成が全置換された）。"""


@dataclass(frozen=True)
class ProjectSnapshot:
    """更新確定後のproject状態の不変スナップショット。"""

    project_id: str
    manga: MangaProject
    revision: int
    generation_epoch: int


@dataclass(frozen=True)
class MutationResult(Generic[T]):
    """mutate系の統一戻り値。tuple戻り値を廃止し、result + snapshotで表現する。"""

    result: T
    project: ProjectSnapshot


def mark_page_dirty(page: Page) -> None:
    """描画入力が変わったページを未レンダリング扱いへ戻す共通処理。

    採用画像の変更・overlay画像/maskの差し替え・レイアウト変更などで使う。
    手続き的に各APIが個別にpendingへ戻すより、共通化した方が漏れに強い。
    """
    page.render_status = "pending"
    page.rendered_at = None
    page.render_asset = None
    page.render_hash = None


def reset_inflight_generation_state(manga: MangaProject) -> None:
    """epoch更新後へ旧jobのactive表示を持ち越さない。"""
    for page in manga.pages:
        for panel in page.panels:
            generation = panel.generation
            # statusに関係なく旧epochの所有権を外す。
            # 生成中に候補採用でdoneへ移ったpanelに古いactive_job_idが残るのを防ぐ。
            generation.active_job_id = None
            generation.prompt_id = None
            if generation.status in {"queued", "running"}:
                generation.status = "pending"
                generation.message = "作品構成の更新により前の生成を中断しました"


def parse_manga(raw: str) -> MangaProject:
    try:
        return MangaProject.model_validate(json.loads(raw))
    except Exception as exc:  # JSON/検証どちらの失敗も保存不能として扱う
        raise InvalidProjectJsonError(f"Manga JSONが不正です: {exc}") from exc


def _find_panel_and_page(manga: MangaProject, panel_id: str) -> tuple[Panel | None, Page | None]:
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return panel, page
    return None, None


_PAGE_RENDER_FIELDS = ("render_status", "rendered_at", "render_asset", "render_hash")


def _worker_panel_scope_fingerprint(
    manga: MangaProject, panel_id: str, owner_page_number: int
) -> str:
    """worker panel mutationで「変更を許可しない範囲」の指紋。

    許可するのは対象panel自身の内容変更と、**所属ページだけ**のrender状態(render_status系)
    だけ。所属ページの対象panelはplaceholderへ置換し、内容変化は無視しつつ「所属ページ・
    list内index・出現回数」は固定する。所属ページのrender状態フィールドはマスクする。

    他ページは一切マスクしない。これにより、
    - 別ページのrender状態をdoneへ偽装する変更
    - 対象panelを別ページへ移動・複製する変更（reading_orderを触らなくても）
    - 他panel・characters/locations・page構造・overlay・project全体の変更
    はすべてfingerprintを変え、:class:`WorkerScopeViolationError` で検出できる。
    """
    data = manga.model_dump(mode="json")
    for page in data["pages"]:
        if page.get("page") != owner_page_number:
            continue
        for field in _PAGE_RENDER_FIELDS:
            page[field] = None
        page["panels"] = [
            {"__worker_target_panel__": True} if panel.get("panel_id") == panel_id else panel
            for panel in page["panels"]
        ]
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


class ProjectMutationService:
    """最新manga_jsonを読み直し、mutateを適用してCAS保存するサービス。

    更新の入口を用途別に分離し、戻り値はすべて :class:`MutationResult` へ統一する。

    - :meth:`mutate_user`: ユーザー起点。``expected_revision`` 必須で、競合時は一度も
      再試行せず即 :class:`ProjectConflictError`（HTTP 409）にする。
    - :meth:`mutate_local`: revision未導入のユーザー操作向け。競合時は読み直して再適用する
      read-modify-writeリトライ（PR2でrevision必須化が進めば削る）。
    - :meth:`mutate_worker`: 生成worker起点。``expected_epoch`` で世代を固定し、競合時は
      読み直して再適用する。世代不一致なら :class:`EpochMismatchError`。
    - :meth:`mutate_worker_panel`: workerの中でも対象panelだけを更新する用途。対象panel以外の
      panelを書き換えたら :class:`WorkerScopeViolationError` で弾く。

    title/work_nameはmanga本体と常に同期する。
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        export_dir: Path,
        repository: ProjectRepository | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.export_dir = export_dir
        self.repository = repository or ProjectRepository()

    def create(self, *, title: str, work_name: str, target_pages: int) -> "ProjectRecord":
        """新規projectを生成し、トランザクションを管理して確定する。

        Router側でSessionを開いてcommitしていた処理をServiceへ寄せる。
        """
        manga = MangaProject(title=title, work_name=work_name, target_pages=target_pages)
        with self.session_factory() as session:
            record = self.repository.create(
                session, title=title, work_name=work_name, manga_json=manga.model_dump_json()
            )
            session.commit()
            session.refresh(record)
            session.expunge(record)
        return record

    def current_epoch(self, project_id: str) -> int:
        with self.session_factory() as session:
            record = self.repository.get(session, project_id)
            if record is None:
                raise ProjectNotFoundError()
            return record.generation_epoch

    def mutate_user(
        self,
        project_id: str,
        *,
        expected_revision: int,
        mutate: Callable[[MangaProject], T],
    ) -> MutationResult[T]:
        """ユーザー起点のCAS更新。競合時は一度も再試行せず即409にする。"""
        with self.session_factory() as session:
            record = self.repository.get(session, project_id)
            if record is None:
                raise ProjectNotFoundError()
            if record.revision != expected_revision:
                raise ProjectRevisionConflictError(project_id, expected_revision)
            base = record.revision
            epoch = record.generation_epoch
            manga = parse_manga(record.manga_json)
            before = manga.model_dump_json()
            result = mutate(manga)
            normalize_manga_assets(manga, self.export_dir)
            if manga.model_dump_json() == before:
                return MutationResult(result, ProjectSnapshot(project_id, manga, base, epoch))
            if self.repository.cas_set_manga(session, project_id, base, manga) == 1:
                session.commit()
                return MutationResult(result, ProjectSnapshot(project_id, manga, base + 1, epoch))
            session.rollback()
            raise ProjectRevisionConflictError(project_id, expected_revision)

    def mutate_local(
        self,
        project_id: str,
        mutate: Callable[[MangaProject], T],
        *,
        attempts: int = 5,
    ) -> MutationResult[T]:
        """revision未導入のユーザー操作向けread-modify-writeリトライ更新。"""
        for _ in range(attempts):
            with self.session_factory() as session:
                record = self.repository.get(session, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                base = record.revision
                epoch = record.generation_epoch
                manga = parse_manga(record.manga_json)
                before = manga.model_dump_json()
                result = mutate(manga)
                normalize_manga_assets(manga, self.export_dir)
                if manga.model_dump_json() == before:
                    return MutationResult(result, ProjectSnapshot(project_id, manga, base, epoch))
                if self.repository.cas_set_manga(session, project_id, base, manga) == 1:
                    session.commit()
                    return MutationResult(
                        result, ProjectSnapshot(project_id, manga, base + 1, epoch)
                    )
                session.rollback()
        raise ProjectConflictError(project_id)

    def mutate_worker(
        self,
        project_id: str,
        *,
        expected_epoch: int,
        mutate: Callable[[MangaProject], T],
        attempts: int = 5,
    ) -> MutationResult[T]:
        """生成worker起点の更新。世代を固定し、競合時は読み直して再適用する。"""
        for _ in range(attempts):
            with self.session_factory() as session:
                record = self.repository.get(session, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                if record.generation_epoch != expected_epoch:
                    raise EpochMismatchError()
                base = record.revision
                manga = parse_manga(record.manga_json)
                before = manga.model_dump_json()
                result = mutate(manga)
                normalize_manga_assets(manga, self.export_dir)
                if manga.model_dump_json() == before:
                    return MutationResult(
                        result, ProjectSnapshot(project_id, manga, base, expected_epoch)
                    )
                if (
                    self.repository.cas_set_manga(
                        session, project_id, base, manga, require_epoch=expected_epoch
                    )
                    == 1
                ):
                    session.commit()
                    return MutationResult(
                        result, ProjectSnapshot(project_id, manga, base + 1, expected_epoch)
                    )
                session.rollback()
        raise ProjectConflictError(project_id)

    def mutate_worker_panel(
        self,
        project_id: str,
        *,
        panel_id: str,
        expected_epoch: int,
        mutate: Callable[[MangaProject, Panel, Page], T],
        attempts: int = 5,
    ) -> MutationResult[T]:
        """worker起点で「対象panelだけ」を更新する。

        mutateは ``(manga, panel, page)`` を受け取る。mangaは生成入力hash算出など読み取り
        専用の文脈で、書き込みは対象panelとページのrender状態(render_status系)に限る。
        対象panel以外のpanel・page構造・overlay・project全体を書き換えたら
        :class:`WorkerScopeViolationError` で弾く。
        """
        for _ in range(attempts):
            with self.session_factory() as session:
                record = self.repository.get(session, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                if record.generation_epoch != expected_epoch:
                    raise EpochMismatchError()
                base = record.revision
                manga = parse_manga(record.manga_json)
                panel, page = _find_panel_and_page(manga, panel_id)
                if panel is None or page is None:
                    raise PanelNotFoundError()
                # 所属ページをmutate前に固定し、スコープ判定の基準にする。
                owner_page_number = page.page
                before = manga.model_dump_json()
                scope_before = _worker_panel_scope_fingerprint(manga, panel_id, owner_page_number)
                result = mutate(manga, panel, page)
                # normalize前にスコープ判定する。normalizeはasset正規化のためのシステム処理で、
                # mutate自身の更新範囲には含めない。
                if (
                    _worker_panel_scope_fingerprint(manga, panel_id, owner_page_number)
                    != scope_before
                ):
                    raise WorkerScopeViolationError()
                normalize_manga_assets(manga, self.export_dir)
                if manga.model_dump_json() == before:
                    return MutationResult(
                        result, ProjectSnapshot(project_id, manga, base, expected_epoch)
                    )
                if (
                    self.repository.cas_set_manga(
                        session, project_id, base, manga, require_epoch=expected_epoch
                    )
                    == 1
                ):
                    session.commit()
                    return MutationResult(
                        result, ProjectSnapshot(project_id, manga, base + 1, expected_epoch)
                    )
                session.rollback()
        raise ProjectConflictError(project_id)

    def replace(
        self,
        project_id: str,
        replacement: MangaProject,
        *,
        expected_revision: int,
        increment_epoch: bool = False,
    ) -> MutationResult[None]:
        """全文を必須revision付きCASで置換する。

        全文保存で暗黙にDB上の最新revisionを期待値へ採用すると、古いクライアントの
        payloadを正当化してしまう。期待値は必ず呼び出し元から受け取る。
        """

        if increment_epoch:
            reset_inflight_generation_state(replacement)
        normalize_manga_assets(replacement, self.export_dir)
        with self.session_factory() as session:
            record = self.repository.get(session, project_id)
            if record is None:
                raise ProjectNotFoundError()
            if record.revision != expected_revision:
                raise ProjectReplaceConflictError(project_id, expected_revision)
            new_epoch = record.generation_epoch + 1 if increment_epoch else record.generation_epoch
            if (
                self.repository.cas_set_manga(
                    session,
                    project_id,
                    expected_revision,
                    replacement,
                    increment_epoch=increment_epoch,
                )
                != 1
            ):
                session.rollback()
                raise ProjectReplaceConflictError(project_id, expected_revision)
            if increment_epoch:
                self.repository.cancel_jobs_before_epoch(session, project_id, new_epoch)
            session.commit()
        return MutationResult(
            None, ProjectSnapshot(project_id, replacement, expected_revision + 1, new_epoch)
        )

    def replace_with_history(
        self,
        project_id: str,
        build_replacement: Callable[[Session, MangaProject], MangaProject],
        *,
        expected_revision: int,
        history_label: str,
    ) -> MutationResult[None]:
        """構造全置換と直前履歴の保存を同一トランザクションでCASする。"""
        with self.session_factory() as session:
            record = self.repository.get(session, project_id)
            if record is None:
                raise ProjectNotFoundError()
            if record.revision != expected_revision:
                raise ProjectReplaceConflictError(project_id, expected_revision)
            new_epoch = record.generation_epoch + 1
            previous_json = record.manga_json
            replacement = build_replacement(session, parse_manga(previous_json))
            reset_inflight_generation_state(replacement)
            normalize_manga_assets(replacement, self.export_dir)
            if (
                self.repository.cas_set_manga(
                    session,
                    project_id,
                    expected_revision,
                    replacement,
                    increment_epoch=True,
                )
                != 1
            ):
                session.rollback()
                raise ProjectReplaceConflictError(project_id, expected_revision)
            self.repository.cancel_jobs_before_epoch(session, project_id, new_epoch)
            self.repository.add_revision_history(session, project_id, history_label, previous_json)
            session.commit()
        return MutationResult(
            None, ProjectSnapshot(project_id, replacement, expected_revision + 1, new_epoch)
        )
