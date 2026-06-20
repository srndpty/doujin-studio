from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

WINDOWS_FONT_DIR = Path("C:/Windows/Fonts")
USER_FONT_DIR = Path.home() / "AppData/Local/Microsoft/Windows/Fonts"

# 源暎アンチック（同人写植の定番フォント）。配布物によりファイル名が揺れるため複数候補を持つ。
GENEI_ANTIQUE_FILES = [
    "GenEiAntiqueNv5-M.ttf",
    "GenEiAntique_v5-M.ttf",
    "GenEiAntiqueNv5-Medium.ttf",
    "GenEiAntiqueP-Medium.ttf",
    "GenEiAntique-Medium.ttf",
    "源暎アンチック.ttf",
    "源暎アンチックv5.ttf",
]
GENEI_ANTIQUE_KEYWORDS = ["geneiantique", "源暎アンチック", "genei_antique", "genei-antique"]

# 退避フォント。BIZ UDゴシックを最優先にする。
FALLBACK_FONT_FILES = [
    "BIZ-UDGothicR.ttc",
    "BIZ-UDPGothicR.ttc",
    "YuGothR.ttc",
    "YuGothM.ttc",
    "meiryo.ttc",
    "msgothic.ttc",
]
BOLD_FONT_FILES = [
    "BIZ-UDGothicB.ttc",
    "YuGothB.ttc",
    "msgothic.ttc",
]


def _font_dirs() -> list[Path]:
    return [path for path in (WINDOWS_FONT_DIR, USER_FONT_DIR) if path.exists()]


def _find_in_dirs(filenames: list[str]) -> Path | None:
    for directory in _font_dirs():
        for filename in filenames:
            candidate = directory / filename
            if candidate.exists():
                return candidate
    return None


def _scan_for_keywords(keywords: list[str]) -> Path | None:
    for directory in _font_dirs():
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                continue
            name = entry.name.casefold()
            if any(keyword.casefold() in name for keyword in keywords):
                return entry
    return None


@lru_cache(maxsize=1)
def find_genei_antique() -> Path | None:
    """源暎アンチックのフォントパスを探す（未導入ならNone）。"""
    direct = _find_in_dirs(GENEI_ANTIQUE_FILES)
    if direct:
        return direct
    return _scan_for_keywords(GENEI_ANTIQUE_KEYWORDS)


@lru_cache(maxsize=1)
def find_dialogue_font_path() -> Path | None:
    """台詞フォント（源暎アンチック優先、無ければBIZ UDゴシック等）のパス。"""
    return find_genei_antique() or _find_in_dirs(FALLBACK_FONT_FILES)


def dialogue_font_is_primary() -> bool:
    return find_genei_antique() is not None


@lru_cache(maxsize=128)
def _truetype_cached(path_str: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path_str, size)


def load_dialogue_font(size: int) -> ImageFont.ImageFont:
    """台詞用フォントを読み込む。源暎アンチック→BIZ UD→PIL既定の順。"""
    path = find_dialogue_font_path()
    if path is not None:
        return _truetype_cached(str(path), size)
    return ImageFont.load_default()


def load_label_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """UIラベルやコマ番号などの汎用ゴシックフォント。"""
    files = BOLD_FONT_FILES if bold else FALLBACK_FONT_FILES
    path = _find_in_dirs(files)
    if path is not None:
        return _truetype_cached(str(path), size)
    return ImageFont.load_default()


def list_fonts() -> list[dict]:
    """利用可能な主要フォントの一覧（GET /api/fonts用）。"""
    genei = find_genei_antique()
    biz = _find_in_dirs(["BIZ-UDGothicR.ttc"])
    yu = _find_in_dirs(["YuGothR.ttc", "YuGothM.ttc"])
    ms = _find_in_dirs(["msgothic.ttc"])
    return [
        {
            "id": "genei_antique",
            "name": "源暎アンチック",
            "path": str(genei) if genei else "",
            "available": genei is not None,
            "is_primary": True,
        },
        {
            "id": "biz_ud_gothic",
            "name": "BIZ UDゴシック",
            "path": str(biz) if biz else "",
            "available": biz is not None,
            "is_primary": False,
        },
        {
            "id": "yu_gothic",
            "name": "游ゴシック",
            "path": str(yu) if yu else "",
            "available": yu is not None,
            "is_primary": False,
        },
        {
            "id": "ms_gothic",
            "name": "MS ゴシック",
            "path": str(ms) if ms else "",
            "available": ms is not None,
            "is_primary": False,
        },
    ]
