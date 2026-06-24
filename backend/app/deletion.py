"""削除済みprojectへの遅延成果物公開を防ぐ永続filesystem fence。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)
DELETION_FENCE_GLOB = "*.deletion-fence"


def deletion_fence_path(export_dir: Path, project_id: str) -> Path | None:
    """export直下の単一名だけを許し、任意パスをfenceにしない。"""
    if not project_id or project_id in {".", ".."} or project_id != Path(project_id).name:
        return None
    root = export_dir.resolve()
    marker = (root / f"{project_id}.deletion-fence").resolve()
    return marker if marker.parent == root else None


def write_deletion_fence(export_dir: Path, project_id: str) -> bool:
    marker = deletion_fence_path(export_dir, project_id)
    if marker is None:
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(project_id, encoding="utf-8")
        return True
    except OSError:
        logger.exception("project削除fenceを書き込めませんでした: %s", marker)
        return False


def project_is_deletion_fenced(export_dir: Path, project_id: str) -> bool:
    marker = deletion_fence_path(export_dir, project_id)
    return marker is not None and marker.is_file()


def sweep_deletion_fences(export_dir: Path) -> None:
    """fenceが示す削除済みprojectの通常名ディレクトリを起動時に再回収する。"""
    if not export_dir.exists():
        return
    root = export_dir.resolve()
    for marker in root.glob(DELETION_FENCE_GLOB):
        try:
            project_id = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        expected = deletion_fence_path(root, project_id)
        if expected is None or expected != marker.resolve():
            logger.warning("不正なproject削除fenceを無視します: %s", marker)
            continue
        project_dir = (root / project_id).resolve()
        if project_dir.parent != root:
            logger.warning("範囲外のproject削除fenceを無視します: %s", marker)
            continue
        shutil.rmtree(project_dir, ignore_errors=True)
        if project_dir.exists():
            logger.warning("削除fenceの成果物を再回収できませんでした: %s", project_dir)
