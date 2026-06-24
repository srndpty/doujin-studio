"""削除済みprojectへの遅延成果物公開を防ぐ永続filesystem fence。"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)
DELETION_FENCE_SUFFIX = ".deletion-fence"
DELETION_FENCE_GLOB = f"*{DELETION_FENCE_SUFFIX}"


def deletion_fence_path(export_dir: Path, project_id: str) -> Path | None:
    """export直下の単一名だけを許し、任意パスをfenceにしない。"""
    if not project_id or project_id in {".", ".."} or project_id != Path(project_id).name:
        return None
    root = export_dir.resolve()
    marker = (root / f"{project_id}{DELETION_FENCE_SUFFIX}").resolve()
    return marker if marker.parent == root else None


def write_deletion_fence(export_dir: Path, project_id: str) -> bool:
    """fenceを原子的に作成する。本文は持たず、project IDはファイル名で表す。

    中身を信頼しないため、書込み途中クラッシュで空ファイルになっても問題ない。
    既存fenceはそのまま有効とみなす（再削除や復元誤掃除をsweep側のDB確認で防ぐ）。
    """
    marker = deletion_fence_path(export_dir, project_id)
    if marker is None:
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        return True
    except OSError:
        logger.exception("project削除fenceを書き込めませんでした: %s", marker)
        return False


def remove_deletion_fence(export_dir: Path, project_id: str) -> None:
    """fenceを取り除く（DB削除commit失敗時のロールバック等で使う）。"""
    marker = deletion_fence_path(export_dir, project_id)
    if marker is None:
        return
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        logger.warning("project削除fenceを取り除けませんでした: %s", marker)


def project_is_deletion_fenced(export_dir: Path, project_id: str) -> bool:
    marker = deletion_fence_path(export_dir, project_id)
    return marker is not None and marker.is_file()


def _fence_project_id(marker: Path) -> str:
    """fence本文ではなくファイル名からproject IDを復元する。"""
    return marker.name[: -len(DELETION_FENCE_SUFFIX)]


def sweep_deletion_fences(export_dir: Path, project_exists: Callable[[str], bool]) -> None:
    """fenceが示す削除済みprojectの通常名ディレクトリを起動時に再回収する。

    fenceは永続するため、削除前のDBバックアップを復元して同じproject IDが復活した
    場合に成果物を誤削除しうる。project_existsで生存projectを確認し、生きていれば
    fenceを古いものとして取り除くだけにして成果物には触れない。
    """
    if not export_dir.exists():
        return
    root = export_dir.resolve()
    for marker in root.glob(DELETION_FENCE_GLOB):
        project_id = _fence_project_id(marker)
        expected = deletion_fence_path(root, project_id)
        if expected is None or expected != marker.resolve():
            logger.warning("不正なproject削除fenceを無視します: %s", marker)
            continue
        if project_exists(project_id):
            # 復元等でprojectが復活している。fenceは古いので成果物に触れず取り除く。
            logger.info("生存projectのfenceを取り除きます（成果物は保持）: %s", project_id)
            remove_deletion_fence(root, project_id)
            continue
        project_dir = (root / project_id).resolve()
        if project_dir.parent != root:
            logger.warning("範囲外のproject削除fenceを無視します: %s", marker)
            continue
        shutil.rmtree(project_dir, ignore_errors=True)
        if project_dir.exists():
            # 回収できなければfenceを残し、次回起動で再試行する。
            logger.warning("削除fenceの成果物を再回収できませんでした: %s", project_dir)
