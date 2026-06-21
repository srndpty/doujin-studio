"""日本語の縦書き・横書き写植エンジン。

禁則処理、句読点・小書き仮名の位置調整、長音や括弧の縦書き回転、
英数字の縦中横を扱い、テキストを切り捨てずに領域へ収める。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from PIL import Image, ImageDraw, ImageFont

# 行頭禁則（行・列の先頭に置けない文字）。
LINE_START_FORBIDDEN = set(
    "」』）］｝〕〉》】、。，．・？！ゝ々ー〜：；,.!?）)]｠､｡"
    "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ"
)
# 行末禁則（行・列の末尾に置けない文字）。
LINE_END_FORBIDDEN = set("「『（［｛〔〈《【（([｟")
# 縦書きで90度回転させる文字（長音・ダッシュ・括弧類）。
ROTATE_CHARS = set("ー－—‐-~〜（）()「」『』【】〈〉《》〔〕［］｛｝<>＜＞≪≫…‥")
# 縦書きで右上へ寄せる小書き仮名。
SMALL_KANA_CHARS = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")
# 縦書きでセル右上へ置く句読点。
PUNCT_CHARS = set("、。，．")


@dataclass
class TextLayout:
    font_size: int
    vertical: bool
    columns: list[list[tuple[str, str]]]  # 縦書きは列、横書きは行。各要素は(kind, text)トークン。
    cell: float  # 主方向（縦書きなら縦）のセル送り。
    advance: float  # 副方向（縦書きなら列間）の送り。
    fits: bool
    width: float
    height: float
    warnings: list[str] = field(default_factory=list)


def _is_tcy_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "%#&+=")


def tokenize_vertical(text: str) -> list[tuple[str, str]]:
    """縦書き用に1セル=1トークンへ分解する。改行は('break','')。"""
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == "\n":
            tokens.append(("break", ""))
            index += 1
            continue
        if _is_tcy_char(ch):
            end = index
            while end < length and _is_tcy_char(text[end]):
                end += 1
            run = text[index:end]
            pos = 0
            while pos < len(run):
                group = run[pos : pos + 2]
                tokens.append(("tcy", group))
                pos += len(group)
            index = end
            continue
        if ch in ROTATE_CHARS:
            tokens.append(("rot", ch))
        else:
            tokens.append(("cjk", ch))
        index += 1
    return tokens


def tokenize_horizontal(text: str) -> list[tuple[str, str]]:
    """横書きは文字単位（改行のみ特別扱い）。"""
    tokens: list[tuple[str, str]] = []
    for ch in text:
        if ch == "\n":
            tokens.append(("break", ""))
        else:
            tokens.append(("cjk", ch))
    return tokens


def _apply_kinsoku(lines: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    """行末禁則の追い出し（開き括弧などを次行へ送る）を行う。"""
    result = [list(line) for line in lines]
    for i in range(len(result) - 1):
        line = result[i]
        while line and line[-1][1][:1] in LINE_END_FORBIDDEN:
            moved = line.pop()
            result[i + 1].insert(0, moved)
        result[i] = line
    return [line for line in result if line]


def wrap_tokens(tokens: list[tuple[str, str]], cells_per_line: int) -> list[list[tuple[str, str]]]:
    """トークン列を1行あたりcells_per_lineで折り返す。行頭禁則は追い込みで吸収する。"""
    cells_per_line = max(1, cells_per_line)
    lines: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for token in tokens:
        if token[0] == "break":
            lines.append(current)
            current = []
            continue
        if len(current) >= cells_per_line:
            head = token[1][:1]
            # 行頭禁則文字は現在行へ追い込む（最大+1セルまで）。
            if head in LINE_START_FORBIDDEN and len(current) <= cells_per_line:
                current.append(token)
                continue
            lines.append(current)
            current = [token]
        else:
            current.append(token)
    if current:
        lines.append(current)
    return _apply_kinsoku(lines)


def layout_text(
    text: str,
    font_path: str | None,
    area_w: float,
    area_h: float,
    vertical: bool,
    default_size: int,
    min_size: int,
    max_lines: int | None = None,
) -> TextLayout:
    """領域へ収まる最大のフォントサイズを選び、行/列構成を返す。

    収まらない場合でも最小サイズで全文を保持し、fits=Falseと警告を返す。
    """
    default_size = max(default_size, min_size)
    chosen: TextLayout | None = None
    for size in range(default_size, min_size - 1, -1):
        layout = _layout_at_size(text, area_w, area_h, vertical, size, max_lines)
        chosen = layout
        if layout.fits:
            return layout
    assert chosen is not None
    chosen.warnings.append("テキストが最小サイズでも吹き出しに収まりません")
    return chosen


def _layout_at_size(
    text: str,
    area_w: float,
    area_h: float,
    vertical: bool,
    size: int,
    max_lines: int | None = None,
) -> TextLayout:
    cell = size * 1.06
    advance = size * 1.12
    if vertical:
        tokens = tokenize_vertical(text)
        cells_per_line = max(1, int(area_h // cell))
        lines = wrap_tokens(tokens, cells_per_line)
        line_count = max(1, len(lines))
        max_cells = max((len(line) for line in lines), default=1)
        width = line_count * advance
        height = max_cells * cell
        fits = (
            width <= area_w + 0.5
            and height <= area_h + 0.5
            and (max_lines is None or line_count <= max_lines)
        )
        return TextLayout(size, True, lines, cell, advance, fits, width, height)
    tokens = tokenize_horizontal(text)
    cells_per_line = max(1, int(area_w // cell))
    lines = wrap_tokens(tokens, cells_per_line)
    line_count = max(1, len(lines))
    max_cells = max((len(line) for line in lines), default=1)
    width = max_cells * cell
    height = line_count * advance
    fits = (
        width <= area_w + 0.5
        and height <= area_h + 0.5
        and (max_lines is None or line_count <= max_lines)
    )
    return TextLayout(size, False, lines, advance, cell, fits, width, height)


def _draw_glyph(
    base: Image.Image,
    font: ImageFont.ImageFont,
    text: str,
    cx: float,
    cy: float,
    fill: tuple[int, int, int],
    stroke_width: int,
    stroke_fill: tuple[int, int, int],
    rotate: bool,
    cell: float = 0.0,
    corner: str = "",
) -> None:
    """1セル分のグリフを中心(cx, cy)へ描く。

    rotateで90度回転。corner="tr"のときはセル(中心cx,cy・一辺cell)の右上へ寄せる
    （縦書きの句読点向け）。
    """
    pad = stroke_width + 4
    try:
        bbox = font.getbbox(text)
    except Exception:
        bbox = (0, 0, len(text) * 10, 16)
    w = max(1, bbox[2] - bbox[0]) + pad * 2
    h = max(1, bbox[3] - bbox[1]) + pad * 2
    tile = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    tile_draw = ImageDraw.Draw(tile)
    tile_draw.text(
        (pad - bbox[0], pad - bbox[1]),
        text,
        font=font,
        fill=(*fill, 255),
        stroke_width=stroke_width,
        stroke_fill=(*stroke_fill, 255),
    )
    if rotate:
        tile = tile.rotate(-90, expand=True)
    if corner == "tr" and cell:
        # セル右上に寄せる（縦書きの句読点）。
        paste_x = int(cx + cell / 2 - tile.width - cell * 0.04)
        paste_y = int(cy - cell / 2 + cell * 0.04)
    else:
        paste_x = int(cx - tile.width / 2)
        paste_y = int(cy - tile.height / 2)
    base.alpha_composite(tile, (paste_x, paste_y))


def draw_layout(
    base_rgba: Image.Image,
    layout: TextLayout,
    font_path: str | None,
    area: tuple[float, float, float, float],
    fill: tuple[int, int, int],
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int] = (255, 255, 255),
    rtl: bool = True,
) -> None:
    """layout_textの結果をRGBA画像へ描画する。areaは(left, top, right, bottom)。"""
    left, top, right, bottom = area
    size = layout.font_size
    full = _load(font_path, size)
    small = _load(font_path, max(8, int(size * 0.62)))
    block_w = layout.width
    block_h = layout.height
    if layout.vertical:
        # 列は右から左。縦方向は上寄せ、横方向は領域中央に寄せる。
        start_right = right - max(0.0, (right - left - block_w) / 2)
        for line_index, line in enumerate(layout.columns):
            col_cx = start_right - layout.advance * (line_index + 0.5)
            for cell_index, (kind, glyph_text) in enumerate(line):
                cell_cy = top + layout.cell * (cell_index + 0.5)
                _draw_cell_vertical(
                    base_rgba,
                    full,
                    small,
                    kind,
                    glyph_text,
                    col_cx,
                    cell_cy,
                    layout.cell,
                    fill,
                    stroke_width,
                    stroke_fill,
                )
    else:
        start_top = top + max(0.0, (bottom - top - block_h) / 2)
        for line_index, line in enumerate(layout.columns):
            line_cy = start_top + layout.cell * (line_index + 0.5)
            line_w = len(line) * layout.advance
            line_left = left + max(0.0, (right - left - line_w) / 2)
            for cell_index, (_kind, glyph_text) in enumerate(line):
                cell_cx = line_left + layout.advance * (cell_index + 0.5)
                _draw_glyph(
                    base_rgba,
                    full,
                    glyph_text,
                    cell_cx,
                    line_cy,
                    fill,
                    stroke_width,
                    stroke_fill,
                    rotate=False,
                )


def _draw_cell_vertical(
    base, full_font, small_font, kind, glyph_text, cx, cy, cell, fill, stroke_width, stroke_fill
):
    if kind == "tcy":
        # 縦中横: 短い英数字を横並びでセル中央へ。
        _draw_glyph(
            base, small_font, glyph_text, cx, cy, fill, stroke_width, stroke_fill, rotate=False
        )
        return
    if kind == "rot":
        _draw_glyph(
            base, full_font, glyph_text, cx, cy, fill, stroke_width, stroke_fill, rotate=True
        )
        return
    ch = glyph_text
    if ch in PUNCT_CHARS:
        # 句読点はセル右上へ置く（縦書きの自然な配置）。
        _draw_glyph(
            base,
            full_font,
            ch,
            cx,
            cy,
            fill,
            stroke_width,
            stroke_fill,
            rotate=False,
            cell=cell,
            corner="tr",
        )
        return
    if ch in SMALL_KANA_CHARS:
        # 小書き仮名は軽く右上へ寄せる。
        _draw_glyph(
            base,
            full_font,
            ch,
            cx + cell * 0.1,
            cy - cell * 0.1,
            fill,
            stroke_width,
            stroke_fill,
            rotate=False,
        )
        return
    _draw_glyph(base, full_font, ch, cx, cy, fill, stroke_width, stroke_fill, rotate=False)


_FONT_CACHE: dict[tuple[str | None, int], ImageFont.ImageFont] = {}


def _load(font_path: str | None, size: int) -> ImageFont.ImageFont:
    key = (font_path, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    if font_path:
        font = cast(ImageFont.ImageFont, ImageFont.truetype(font_path, size))
    else:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font
