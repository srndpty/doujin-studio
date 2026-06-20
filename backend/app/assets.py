from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .schemas import MangaProject

_UNSAFE_COMPONENT = re.compile(r"[^0-9A-Za-z._-]+")


def safe_component(value: str, fallback: str = "asset") -> str:
    """URLや識別子をファイル名へ安全に埋め込める形へ変換する。"""
    cleaned = _UNSAFE_COMPONENT.sub("_", value).strip(" ._")
    return (cleaned or fallback)[:120]


def stable_asset_name(value: str, kind: str, suffix: str = "") -> str:
    """可読部分が衝突しても一意性を保つ安定ファイル名を返す。"""
    readable = safe_component(value, kind)[:64]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    suffix_part = f"-{safe_component(suffix, 'asset')}" if suffix else ""
    return f"{readable}-{digest}{suffix_part}.png"


def path_to_asset_id(path: Path, export_dir: Path) -> str:
    root = export_dir.resolve()
    target = path.resolve()
    if not target.is_relative_to(root):
        raise ValueError("アセットは出力フォルダ配下に配置してください")
    return target.relative_to(root).as_posix()


def resolve_asset_path(asset_id: str, export_dir: Path) -> Path:
    root = export_dir.resolve()
    raw = Path(asset_id)
    if raw.is_absolute():
        target = raw.resolve()
    else:
        normalized = asset_id.replace("\\", "/").lstrip("/")
        prefix = f"{export_dir.name}/"
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
        target = (root / normalized).resolve()
    if not target.is_relative_to(root):
        raise ValueError("アセットIDが出力フォルダ外を参照しています")
    return target


def normalize_asset_id(value: str | None, export_dir: Path) -> str | None:
    if not value:
        return value
    try:
        return path_to_asset_id(resolve_asset_path(value, export_dir), export_dir)
    except ValueError:
        return value.replace("\\", "/")


def normalize_manga_assets(manga: MangaProject, export_dir: Path) -> MangaProject:
    """旧絶対パスを含むManga JSONを相対POSIX IDへ移行する。"""
    for character in manga.characters:
        character.reference_image_asset = normalize_asset_id(
            character.reference_image_asset, export_dir
        )
    for location in manga.locations:
        location.reference_image_asset = normalize_asset_id(
            location.reference_image_asset, export_dir
        )
    for page in manga.pages:
        for overlay in page.overlay_elements:
            overlay.asset = normalize_asset_id(overlay.asset, export_dir)
            overlay.mask_asset = normalize_asset_id(overlay.mask_asset, export_dir)
        for panel in page.panels:
            panel.image_asset = normalize_asset_id(panel.image_asset, export_dir)
            for candidate in panel.image_candidates:
                candidate.asset = normalize_asset_id(candidate.asset, export_dir) or candidate.asset
                for reference in candidate.reference_images:
                    reference.asset = (
                        normalize_asset_id(reference.asset, export_dir) or reference.asset
                    )
            for control_reference in panel.control_references:
                control_reference.asset = (
                    normalize_asset_id(control_reference.asset, export_dir)
                    or control_reference.asset
                )
            for generation_reference in panel.generation.reference_images:
                generation_reference.asset = (
                    normalize_asset_id(generation_reference.asset, export_dir)
                    or generation_reference.asset
                )
    return manga
