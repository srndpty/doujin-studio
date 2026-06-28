"""描画・不変アセット公開・本番状態判定を集約するレンダリング層。

main.py から「snapshot描画 → 入力hash付き不変PNG/CBZ公開 → revision/epoch/hash確認後の
done確定 → 要求所有権に基づく競合cleanup → production status」を移す。純粋関数は
module-levelに置き、DB/トランザクションを伴う確定・cleanupは RenderingService に持たせる。
domain層はHTTPExceptionに依存せず、HTTP変換は呼び出し側(router/main)が担う。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from .asset_storage import iter_manga_asset_strings, publish_immutable_asset
from .assets import path_to_asset_id, resolve_asset_path
from .database import ProjectRevisionRecord, now_utc
from .mutation import (
    ProjectConflictError,
    ProjectMutationService,
    ProjectSnapshot,
    RenderCommitConflictError,
)
from .renderer import (
    export_cbz,
    render_project_page,
    render_project_pages,
    sanitize_export_filename,
)
from .repository import ProjectRepository
from .schemas import (
    MangaProject,
    PageProductionStatus,
    ProjectProductionStatus,
)

logger = logging.getLogger(__name__)

ProductionStatusValue = Literal["incomplete", "ready", "complete"]

# 描画エンジンのバージョン。typeset/renderer のロジックを変えてピクセル出力が変わったら
# 必ず +1 する。ページ成果物のファイル名は入力hash由来のため、コード変更だけでは
# hashが変わらず、同名・別内容の不変アセット衝突（RuntimeError: 不変アセットの内容が
# 一致しません）を起こす。本バージョンをシグネチャへ含めることで、コード変更後は新しい
# ファイル名で再公開され、古いキャッシュページと衝突しなくなる。
# 履歴: 1=初期, 2=縦書き句読点を右上クロップ配置・全角チルダ等の回転対応,
#       3=波ダッシュの向き反転・三点リーダの実インク中央寄せ・吹き出しのアンチエイリアス
RENDER_ENGINE_VERSION = 3


class RenderInputChangedError(Exception):
    pass


class InconsistentSelectedPanelError(Exception):
    """採用candidate指定済みなのにassetが欠損/不整合なpanelがある。"""

    def __init__(self, panel_id: str) -> None:
        super().__init__(panel_id)
        self.panel_id = panel_id


@dataclass(frozen=True)
class RenderedPage:
    """単一ページ描画・確定の戻り値。位置展開tupleを廃止し意味を明示する。"""

    asset: Path
    warnings: list[str]
    project: ProjectSnapshot


def asset_to_id(path: Path, export_dir: Path) -> str:
    return path_to_asset_id(path, export_dir)


def selected_panel_asset_is_valid(panel, export_dir: Path) -> bool:
    """採用candidateと実際に描画するimage_assetが整合し、ファイルが存在するか。

    generation_epochは構造変更しか検出しないため、候補選択・asset消失・候補削除などの
    非構造編集ではプレースホルダを完成画像として確定し得る。これを明示的に弾く。
    """
    if not panel.selected_candidate_id or not panel.image_asset:
        return False
    candidate = next(
        (item for item in panel.image_candidates if item.id == panel.selected_candidate_id),
        None,
    )
    if candidate is None or panel.image_asset != candidate.asset:
        return False
    try:
        return resolve_asset_path(panel.image_asset, export_dir).is_file()
    except ValueError:
        return False


def find_inconsistent_selected_panel(manga: MangaProject, export_dir: Path):
    """採用candidate指定済みなのにassetが欠損/不整合なpanelを返す（無ければNone）。"""
    for page in manga.pages:
        inconsistent = find_inconsistent_selected_panel_for_page(manga, page.page, export_dir)
        if inconsistent is not None:
            return inconsistent
    return None


def find_inconsistent_selected_panel_for_page(
    manga: MangaProject, page_number: int, export_dir: Path
):
    """対象ページ内で、採用candidate指定済みなのにassetが欠損/不整合なpanelを返す。"""
    page = next((item for item in manga.pages if item.page == page_number), None)
    if page is None:
        return None
    for panel in page.panels:
        if panel.selected_candidate_id and not selected_panel_asset_is_valid(panel, export_dir):
            return panel
    return None


def page_render_is_valid(manga: MangaProject, page, export_dir: Path) -> bool:
    if page.render_status != "done" or not page.render_asset or not page.render_hash:
        return False
    if page.render_hash != page_render_hash(manga, page):
        return False
    expected_name = f"page_{page.page:03d}.{page.render_hash}.png"
    try:
        path = resolve_asset_path(page.render_asset, export_dir)
    except ValueError:
        return False
    return path.name == expected_name and path.is_file()


def migrate_legacy_render_state(manga: MangaProject, export_dir: Path) -> bool:
    """不変render assetが欠損・不整合なdoneページをpendingへ移行する。"""
    from .mutation import mark_page_dirty

    changed = False
    for page in manga.pages:
        if page.render_status == "done" and not page_render_is_valid(manga, page, export_dir):
            mark_page_dirty(page)
            changed = True
    return changed


def build_production_status(
    project_id: str, manga: MangaProject, export_dir: Path
) -> ProjectProductionStatus:
    page_statuses: list[PageProductionStatus] = []
    project_blockers: list[str] = []
    adopted_total = 0
    panel_total = 0
    rendered_pages = 0
    for page in manga.pages:
        total = len(page.panels)
        # 採用画像は「選択済み かつ assetが整合してファイルが存在する」ものだけ数える。
        adopted = sum(
            1 for panel in page.panels if selected_panel_asset_is_valid(panel, export_dir)
        )
        rendered = page_render_is_valid(manga, page, export_dir)
        blockers: list[str] = []
        for panel in page.panels:
            if not panel.selected_candidate_id:
                blockers.append(f"{panel.panel_id}: 採用画像が未選択です")
            elif not selected_panel_asset_is_valid(panel, export_dir):
                blockers.append(f"{panel.panel_id}: 採用画像が欠損しています")
        if not rendered:
            blockers.append(f"{page.page}ページ: ページが未レンダリングです")
        status: ProductionStatusValue
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
    project_status: ProductionStatusValue
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


def _font_render_signature(manga: MangaProject) -> dict:
    """描画に使う台詞・擬音フォントの識別子(パス+mtime)。

    フォントを差し替え/追加すると同じManga JSONでも描画結果が変わるため、これを
    シグネチャへ含めて再描画を促し、不変アセットの内容不一致を防ぐ（P1）。
    """
    from .fonts import find_dialogue_font_path, find_sfx_font_path

    def signature(path) -> list | None:
        if path is None:
            return None
        try:
            return [str(path), int(path.stat().st_mtime)]
        except OSError:
            return [str(path), None]

    return {
        "dialogue": signature(find_dialogue_font_path(manga.typography.primary_font)),
        "sfx": signature(find_sfx_font_path()),
    }


def page_render_signature(manga: MangaProject, page) -> str:
    """ページの描画結果に影響する入力だけを取り出した安定シグネチャ。

    一致する限り既存のレンダリング状態を保持し、台詞・レイアウトなど
    画像に影響しないメタ変更では再レンダリングを促さない。
    """
    payload = {
        "render_engine_version": RENDER_ENGINE_VERSION,
        "typography": manga.typography.model_dump(),
        "page_layout": manga.page_layout.model_dump(),
        "reading_direction": manga.reading_direction,
        "fonts": _font_render_signature(manga),
        "overlays": [overlay.model_dump() for overlay in page.overlay_elements],
        "panels": [
            {
                "panel_id": panel.panel_id,
                "bbox": panel.bbox,
                "image_asset": panel.image_asset,
                "dialogue": [line.model_dump() for line in panel.dialogue],
                "sfx": [item.model_dump() for item in panel.sfx],
                # しっぽ方向はcharacter_layoutのregion_box中心（無ければposition）から決まるため
                # 描画入力に含める（P2）。
                "character_layout": [
                    {"id": entry.id, "position": entry.position, "region_box": entry.region_box}
                    for entry in panel.character_layout
                ],
                "crop": {
                    "fit_mode": panel.generation.fit_mode,
                    "crop_anchor": panel.generation.crop_anchor,
                    "crop_scale": panel.generation.crop_scale,
                    "crop_offset_x": panel.generation.crop_offset_x,
                    "crop_offset_y": panel.generation.crop_offset_y,
                    "focal_x": panel.generation.focal_x,
                    "focal_y": panel.generation.focal_y,
                },
            }
            for panel in page.panels
        ],
    }
    return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


def structure_signature(manga: MangaProject) -> tuple:
    """生成ジョブの意味論を変えるページ・コマ構造だけを比較する。"""
    return tuple(
        (
            page.page,
            tuple(panel.panel_id for panel in page.panels),
            tuple(page.reading_order),
        )
        for page in manga.pages
    )


def page_render_hash(manga: MangaProject, page) -> str:
    return hashlib.sha256(page_render_signature(manga, page).encode("utf-8")).hexdigest()[:20]


def invalidate_changed_pages(payload: MangaProject, previous: MangaProject) -> None:
    """描画入力が変わったページのみpendingにし、それ以外は前回状態を引き継ぐ。"""
    from .mutation import mark_page_dirty

    previous_by_number = {page.page: page for page in previous.pages}
    previous_signatures = {
        page.page: page_render_signature(previous, page) for page in previous.pages
    }
    for page in payload.pages:
        old_page = previous_by_number.get(page.page)
        if old_page is not None and previous_signatures.get(page.page) == page_render_signature(
            payload, page
        ):
            page.render_status = old_page.render_status
            page.rendered_at = old_page.rendered_at
            page.render_asset = old_page.render_asset
            page.render_hash = old_page.render_hash
        else:
            mark_page_dirty(page)


def render_snapshot_page(
    project_id: str,
    manga: MangaProject,
    page_number: int,
    export_dir: Path,
    revision: int,
    ownership: dict[Path, bool] | None = None,
) -> tuple[Path, list[str]]:
    """snapshotを一時描画し、入力hash付き不変PNGへ昇格する。DB状態は変更しない。"""
    staging = (
        export_dir / project_id / ".render-staging" / f"revision-{revision}-{uuid.uuid4().hex}"
    )
    try:
        staged, warnings = render_project_page(
            project_id, manga, page_number, export_dir, output_dir=staging
        )
        page = next(item for item in manga.pages if item.page == page_number)
        render_hash = page_render_hash(manga, page)
        target = export_dir / project_id / "pages" / f"page_{page_number:03d}.{render_hash}.png"
        created = publish_immutable_asset(staged, target)
        if ownership is not None:
            ownership[target] = created
        return target, warnings
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def render_snapshot_pages(
    project_id: str,
    manga: MangaProject,
    export_dir: Path,
    revision: int,
    ownership: dict[Path, bool] | None = None,
) -> tuple[list[Path], list[str]]:
    """全ページ成功後に入力hash付き不変PNG群へ昇格する。DB状態は変更しない。"""
    staging = (
        export_dir / project_id / ".render-staging" / f"revision-{revision}-{uuid.uuid4().hex}"
    )
    try:
        staged_assets, warnings = render_project_pages(
            project_id, manga, export_dir, output_dir=staging
        )
        target_dir = export_dir / project_id / "pages"
        target_dir.mkdir(parents=True, exist_ok=True)
        assets: list[Path] = []
        pages = {page.page: page for page in manga.pages}
        for staged in staged_assets:
            page_number = int(staged.stem.removeprefix("page_"))
            render_hash = page_render_hash(manga, pages[page_number])
            target = target_dir / f"page_{page_number:03d}.{render_hash}.png"
            created = publish_immutable_asset(staged, target)
            if ownership is not None:
                ownership[target] = created
            assets.append(target)
        return assets, warnings
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def export_confirmed_cbz(
    project_id: str,
    title: str,
    page_assets: list[Path],
    export_dir: Path,
    revision: int,
    ownership: dict[Path, bool] | None = None,
) -> Path:
    """CBZを確定revisionの一時パスで完成させてから正規出力へ昇格する。"""
    staging = export_dir / project_id / ".cbz-staging" / f"revision-{revision}-{uuid.uuid4().hex}"
    try:
        staged = export_cbz(project_id, title, page_assets, export_dir, output_dir=staging)
        manifest_hash = hashlib.sha256(
            "\n".join(asset.name for asset in sorted(page_assets)).encode("utf-8")
        ).hexdigest()[:16]
        target = (
            export_dir
            / project_id
            / (
                f"{sanitize_export_filename(title)}-r{revision}-{manifest_hash}-"
                f"{uuid.uuid4().hex}.cbz"
            )
        )
        created = publish_immutable_asset(staged, target)
        if ownership is not None:
            ownership[target] = created
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class RenderingService:
    """描画確定・参照走査・競合cleanupなどDBを伴うレンダリング操作。"""

    def __init__(
        self,
        session_factory: sessionmaker,
        export_dir: Path,
        mutation: ProjectMutationService,
        repository: ProjectRepository | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.export_dir = export_dir
        self.mutation = mutation
        self.repository = repository or ProjectRepository()

    def referenced_project_asset_paths(self, project_id: str) -> set[Path]:
        with self.session_factory() as session:
            record = self.repository.get(session, project_id)
            if record is None:
                return set()
            raw_documents = [record.manga_json]
            raw_documents.extend(
                manga_json
                for (manga_json,) in session.query(ProjectRevisionRecord.manga_json)
                .filter(ProjectRevisionRecord.project_id == project_id)
                .all()
            )
        paths: set[Path] = set()
        for raw in raw_documents:
            try:
                manga = MangaProject.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError):
                continue
            for value in iter_manga_asset_strings(manga):
                try:
                    path = resolve_asset_path(value, self.export_dir)
                except ValueError:
                    continue
                # 参照確認はcleanupの補助。is_fileのOSError等で本来の409/502を上書きしない。
                try:
                    if path.is_file():
                        paths.add(path)
                except OSError:
                    continue
        return paths

    def cleanup_published_assets(self, project_id: str, ownership: dict[Path, bool]) -> None:
        """この要求が作成し、current/historyのどちらからも未参照な成果物だけ回収する。

        referenced側は常にcanonical absolute path。ownership側は相対EXPORT_DIR下だと相対の
        ことがあるため、必ずresolve()して同じ基準で比較する（相対パスのまま比較すると
        参照中assetを誤って削除する）。
        """
        referenced = self.referenced_project_asset_paths(project_id)
        for path, created_by_request in ownership.items():
            target = path.resolve()
            if created_by_request and target not in referenced:
                # cleanupは補助処理。unlink失敗で本来の409/502を上書きしないよう吸収する。
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    logger.warning("cleanup unlink失敗: %s", target, exc_info=True)

    def commit_rendered_pages(
        self,
        project_id: str,
        snapshot: MangaProject,
        assets: list[Path],
        *,
        expected_revision: int | None = None,
        expected_epoch: int | None = None,
    ) -> ProjectSnapshot:
        """最新入力がsnapshotと一致する場合だけdoneと不変assetをCAS確定する。

        確定不能時は RenderInputChangedError / RenderCommitConflictError /
        EpochMismatchError を送出する（HTTP変換は呼び出し側）。
        """
        snapshot_pages = {page.page: page for page in snapshot.pages}
        asset_by_page = {
            int(asset.name.split(".")[0].removeprefix("page_")): asset for asset in assets
        }

        def finalize(latest: MangaProject) -> None:
            latest_pages = {page.page: page for page in latest.pages}
            for page_number, snapshot_page in snapshot_pages.items():
                latest_page = latest_pages.get(page_number)
                asset = asset_by_page.get(page_number)
                if (
                    latest_page is None
                    or asset is None
                    or page_render_hash(latest, latest_page)
                    != page_render_hash(snapshot, snapshot_page)
                ):
                    raise RenderInputChangedError()
            for page_number, snapshot_page in snapshot_pages.items():
                latest_page = latest_pages[page_number]
                latest_page.render_status = "done"
                latest_page.rendered_at = now_utc()
                latest_page.render_hash = page_render_hash(snapshot, snapshot_page)
                latest_page.render_asset = asset_to_id(asset_by_page[page_number], self.export_dir)

        # revision競合は描画確定固有メッセージを維持する（PR1のエラーメッセージ互換）。
        # RenderInputChangedError / EpochMismatchError はそのまま伝播させる。
        try:
            if expected_revision is not None:
                result = self.mutation.mutate_user(
                    project_id, expected_revision=expected_revision, mutate=finalize
                )
            elif expected_epoch is not None:
                result = self.mutation.mutate_worker(
                    project_id, expected_epoch=expected_epoch, mutate=finalize
                )
            else:
                result = self.mutation.mutate_local(project_id, finalize)
        except ProjectConflictError as exc:
            raise RenderCommitConflictError() from exc
        return result.project

    def render_and_commit_page(
        self,
        project_id: str,
        snapshot: MangaProject,
        snapshot_revision: int,
        page_number: int,
    ) -> RenderedPage:
        # 全体render・CBZと同じ不変条件をここに集約する。直接ページrender・候補選択後renderも
        # この経路を通るため、採用candidateとassetが不整合ならプレースホルダを確定しない。
        inconsistent = find_inconsistent_selected_panel_for_page(
            snapshot, page_number, self.export_dir
        )
        if inconsistent is not None:
            raise InconsistentSelectedPanelError(inconsistent.panel_id)
        published_by_request: dict[Path, bool] = {}
        try:
            asset, warnings = render_snapshot_page(
                project_id,
                snapshot,
                page_number,
                self.export_dir,
                snapshot_revision,
                ownership=published_by_request,
            )
            # 対象ページだけを確定するため、snapshotを対象ページへ絞ったCAS用projectにする。
            page = next(item for item in snapshot.pages if item.page == page_number)
            commit_snapshot = snapshot.model_copy(deep=True)
            commit_snapshot.pages = [page.model_copy(deep=True)]
            # 描画中に対象ページ以外が更新されても、page_render_hashが一致する限り最新へ
            # 対象ページだけを安全に再適用する。対象ページが変わればRenderInputChangedError。
            committed = self.commit_rendered_pages(project_id, commit_snapshot, [asset])
        except Exception:
            self.cleanup_published_assets(project_id, published_by_request)
            raise
        return RenderedPage(asset=asset, warnings=warnings, project=committed)
