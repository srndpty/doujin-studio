from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw

from .fonts import find_dialogue_font_path, load_label_font
from .schemas import Dialogue, MangaProject, Page, Panel, Sfx, TypographySettings
from . import typeset

PAGE_SIZE = (1200, 1700)
PANEL_OUTLINE_WIDTH = 4
BUBBLE_INNER_PAD = 16


def render_project_pages(
    project_id: str, manga: MangaProject, export_dir: Path
) -> tuple[list[Path], list[str]]:
    pages_dir = export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []
    warnings: list[str] = []
    for page in manga.pages:
        target, page_warnings = _render_single_page(manga, page, pages_dir)
        assets.append(target)
        warnings.extend(page_warnings)
    return assets, warnings


def render_project_page(
    project_id: str, manga: MangaProject, page_number: int, export_dir: Path
) -> tuple[Path, list[str]]:
    pages_dir = export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    page = next((item for item in manga.pages if item.page == page_number), None)
    if page is None:
        raise ValueError(f"ページが見つかりません: {page_number}")
    return _render_single_page(manga, page, pages_dir)


def _render_single_page(manga: MangaProject, page: Page, pages_dir: Path) -> tuple[Path, list[str]]:
    image = Image.new("RGBA", PAGE_SIZE, (248, 248, 244, 255))
    draw = ImageDraw.Draw(image)
    warnings: list[str] = []
    for panel in page.panels:
        warnings.extend(draw_panel(image, draw, panel, manga.typography, manga.reading_direction))
    draw_page_number(draw, page.page, manga.reading_direction)
    target = pages_dir / f"page_{page.page:03d}.png"
    image.convert("RGB").save(target)
    return target, [f"{page.page}ページ {message}" for message in warnings]


def export_cbz(project_id: str, title: str, page_assets: list[Path], export_dir: Path) -> Path:
    safe_title = sanitize_export_filename(title)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = export_dir / project_id / f"{safe_title}-{timestamp}.cbz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for asset in sorted(page_assets):
            archive.write(asset, asset.name)
    return target


def sanitize_export_filename(title: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    sanitized = re.sub(r"\s+", " ", sanitized)
    return (sanitized or "名称未設定")[:80].rstrip(" .")


def draw_panel(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    typography: TypographySettings,
    reading_direction: str,
) -> list[str]:
    page_w, page_h = PAGE_SIZE
    left = int(panel.bbox[0] * page_w)
    top = int(panel.bbox[1] * page_h)
    width = int(panel.bbox[2] * page_w)
    height = int(panel.bbox[3] * page_h)
    box = (left, top, left + width, top + height)

    if panel.image_asset and Path(panel.image_asset).exists():
        panel_image = Image.open(panel.image_asset).convert("RGB")
        fitted = fit_image_to_box(
            panel_image,
            (width, height),
            panel.generation.fit_mode,
            panel.generation.crop_anchor,
            scale=panel.generation.crop_scale,
            offset_x=panel.generation.crop_offset_x,
            offset_y=panel.generation.crop_offset_y,
            focal=(
                (panel.generation.focal_x, panel.generation.focal_y)
                if panel.generation.focal_x is not None and panel.generation.focal_y is not None
                else None
            ),
        )
        page_image.paste(fitted.convert("RGB"), (left, top))
    else:
        draw.rectangle(box, fill=(230, 232, 235, 255))

    draw.rectangle(box, outline=(20, 20, 20, 255), width=PANEL_OUTLINE_WIDTH)
    label_font = load_label_font(20)
    draw.text((left + 14, top + 10), panel.panel_id, fill=(20, 20, 20, 255), font=label_font)

    warnings: list[str] = []
    for sfx in panel.sfx:
        if sfx.layer == "below":
            draw_sfx(page_image, sfx, box)
    for dialogue in panel.dialogue:
        warnings.extend(draw_dialogue(page_image, draw, dialogue, box, typography))
    for sfx in panel.sfx:
        if sfx.layer == "above":
            draw_sfx(page_image, sfx, box)
    return warnings


# --- 吹き出しと写植 ---

def _anchor_point(position: str, box: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = box
    w = right - left
    h = bottom - top
    points = {
        "upper_left": (left + int(w * 0.27), top + int(h * 0.27)),
        "upper_right": (right - int(w * 0.27), top + int(h * 0.27)),
        "lower_left": (left + int(w * 0.27), bottom - int(h * 0.27)),
        "lower_right": (right - int(w * 0.27), bottom - int(h * 0.27)),
        "center": (left + w // 2, top + h // 2),
    }
    return points.get(position, points["center"])


def _default_bubble(dialogue: Dialogue, panel_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = panel_box
    panel_w = right - left
    panel_h = bottom - top
    if dialogue.box:
        x = left + int(dialogue.box[0] * panel_w)
        y = top + int(dialogue.box[1] * panel_h)
        bw = int(dialogue.box[2] * panel_w)
        bh = int(dialogue.box[3] * panel_h)
        return (x, y, x + bw, y + bh)
    # 縦書きは縦長、横書きは横長の既定サイズにする。
    if dialogue.vertical:
        bw = max(120, min(int(panel_w * 0.34), 360))
        bh = max(160, min(int(panel_h * 0.6), 560))
    else:
        bw = max(200, min(int(panel_w * 0.6), 480))
        bh = max(120, min(int(panel_h * 0.34), 360))
    cx, cy = _anchor_point(dialogue.position, panel_box)
    return (cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2)


def _clamp_box(box: tuple[int, int, int, int], bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    bl, bt, br, bb = bounds
    x0, y0, x1, y1 = box
    w = min(x1 - x0, br - bl)
    h = min(y1 - y0, bb - bt)
    x0 = max(bl, min(x0, br - w))
    y0 = max(bt, min(y0, bb - h))
    return (x0, y0, x0 + w, y0 + h)


def draw_dialogue(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    dialogue: Dialogue,
    panel_box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> list[str]:
    left, top, right, bottom = panel_box
    inset = PANEL_OUTLINE_WIDTH + 4
    bounds = (left + inset, top + inset, right - inset, bottom - inset)
    bubble = _clamp_box(_default_bubble(dialogue, panel_box), bounds)
    font_path = find_dialogue_font_path()
    font_path_str = str(font_path) if font_path else None

    default_size = min(dialogue.font_size, typography.default_font_size or dialogue.font_size)
    default_size = max(default_size, dialogue.min_font_size)
    min_size = dialogue.min_font_size

    def layout_for(target_box: tuple[int, int, int, int]) -> typeset.TextLayout:
        inner_w = (target_box[2] - target_box[0]) - BUBBLE_INNER_PAD * 2
        inner_h = (target_box[3] - target_box[1]) - BUBBLE_INNER_PAD * 2
        return typeset.layout_text(
            dialogue.text, font_path_str, max(8, inner_w), max(8, inner_h),
            dialogue.vertical, default_size, min_size,
        )

    layout = layout_for(bubble)
    if not layout.fits:
        # 吹き出しをコマ内最大まで広げてから再計算する。
        expanded = _clamp_box(bounds, bounds)
        expanded_layout = layout_for(expanded)
        if expanded_layout.fits or expanded_layout.font_size >= layout.font_size:
            bubble, layout = expanded, expanded_layout

    warnings = list(layout.warnings)
    fill = (20, 20, 20)
    stroke_width = 0
    stroke_fill = (255, 255, 255)

    if dialogue.balloon == "none":
        stroke_width = max(3, int(layout.font_size * 0.12))
    else:
        _draw_balloon_shape(page_image, draw, dialogue, bubble, panel_box)

    area = (
        bubble[0] + BUBBLE_INNER_PAD,
        bubble[1] + BUBBLE_INNER_PAD,
        bubble[2] - BUBBLE_INNER_PAD,
        bubble[3] - BUBBLE_INNER_PAD,
    )
    typeset.draw_layout(page_image, layout, font_path_str, area, fill, stroke_width, stroke_fill)
    return [f"{dialogue.speaker}: {message}" for message in warnings]


def _draw_balloon_shape(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    dialogue: Dialogue,
    bubble: tuple[int, int, int, int],
    panel_box: tuple[int, int, int, int],
) -> None:
    outline = (25, 25, 25, 255)
    white = (255, 255, 255, 255)
    x0, y0, x1, y1 = bubble
    if dialogue.balloon == "caption":
        draw.rectangle(bubble, fill=(252, 252, 250, 255), outline=outline, width=3)
        _draw_tail(draw, dialogue, bubble, panel_box, outline, white, square=True)
        return
    if dialogue.balloon == "burst":
        _draw_burst(draw, bubble, outline, white)
        _draw_tail(draw, dialogue, bubble, panel_box, outline, white)
        return
    if dialogue.balloon == "cloud":
        _draw_cloud(draw, bubble, outline, white)
        _draw_cloud_tail(draw, dialogue, bubble, panel_box, outline, white)
        return
    # oval（標準の楕円）
    draw.ellipse(bubble, fill=white, outline=outline, width=3)
    _draw_tail(draw, dialogue, bubble, panel_box, outline, white)


def _tail_tip(dialogue: Dialogue, bubble, panel_box) -> tuple[int, int]:
    left, top, right, bottom = panel_box
    pw, ph = right - left, bottom - top
    if dialogue.tail is not None:
        return (left + int(dialogue.tail.tip[0] * pw), top + int(dialogue.tail.tip[1] * ph))
    # 既定はコマ中央へ向ける（話者方向の代理）。
    bx = (bubble[0] + bubble[2]) / 2
    by = (bubble[1] + bubble[3]) / 2
    cx, cy = (left + pw / 2, top + ph / 2)
    dx, dy = cx - bx, cy - by
    norm = math.hypot(dx, dy) or 1.0
    reach = min(pw, ph) * 0.18
    return (int(bx + dx / norm * (abs(bubble[2] - bubble[0]) / 2 + reach)),
            int(by + dy / norm * (abs(bubble[3] - bubble[1]) / 2 + reach)))


def _draw_tail(draw, dialogue, bubble, panel_box, outline, fill, square: bool = False) -> None:
    if dialogue.tail is not None and not dialogue.tail.enabled:
        return
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tip = _tail_tip(dialogue, bubble, panel_box)
    width_ratio = dialogue.tail.width if dialogue.tail is not None else 0.16
    base_w = max(10, (x1 - x0) * width_ratio)
    # 付け根は吹き出し中心から先端方向の縁に取る。
    dx, dy = tip[0] - cx, tip[1] - cy
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
    base_cx = cx + ux * rx * 0.92
    base_cy = cy + uy * ry * 0.92
    # 縁に沿う垂直方向ベクトル。
    px, py = -uy, ux
    p1 = (base_cx + px * base_w / 2, base_cy + py * base_w / 2)
    p2 = (base_cx - px * base_w / 2, base_cy - py * base_w / 2)
    draw.polygon([p1, p2, tip], fill=fill, outline=outline)


def _draw_cloud_tail(draw, dialogue, bubble, panel_box, outline, fill) -> None:
    if dialogue.tail is not None and not dialogue.tail.enabled:
        return
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tip = _tail_tip(dialogue, bubble, panel_box)
    for i, t in enumerate((0.45, 0.72, 0.95)):
        bx = cx + (tip[0] - cx) * t
        by = cy + (tip[1] - cy) * t
        r = max(4, int((x1 - x0) * 0.05 * (1 - i * 0.28)))
        draw.ellipse((bx - r, by - r, bx + r, by + r), fill=fill, outline=outline, width=2)


def _draw_cloud(draw, bubble, outline, fill) -> None:
    x0, y0, x1, y1 = bubble
    w, h = x1 - x0, y1 - y0
    draw.rounded_rectangle((x0 + w * 0.06, y0 + h * 0.12, x1 - w * 0.06, y1 - h * 0.12),
                           radius=int(min(w, h) * 0.3), fill=fill, outline=outline, width=3)
    # 縁にこぶを並べて雲状にする。
    bumps = max(6, int(w / 60))
    rb = min(w, h) * 0.12
    for i in range(bumps):
        t = i / bumps
        for cxb, cyb in (
            (x0 + w * t, y0 + h * 0.12),
            (x0 + w * t, y1 - h * 0.12),
        ):
            draw.ellipse((cxb - rb, cyb - rb, cxb + rb, cyb + rb), fill=fill, outline=outline, width=2)
    for i in range(max(3, int(h / 60))):
        t = i / max(3, int(h / 60))
        for cxb, cyb in (
            (x0 + w * 0.06, y0 + h * t),
            (x1 - w * 0.06, y0 + h * t),
        ):
            draw.ellipse((cxb - rb, cyb - rb, cxb + rb, cyb + rb), fill=fill, outline=outline, width=2)


def _draw_burst(draw, bubble, outline, fill) -> None:
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
    spikes = 16
    points = []
    for i in range(spikes * 2):
        angle = math.pi * i / spikes
        radius = 1.0 if i % 2 == 0 else 0.74
        points.append((cx + math.cos(angle) * rx * radius, cy + math.sin(angle) * ry * radius))
    draw.polygon(points, fill=fill, outline=outline)


# --- SFX ---

def _parse_color(value: str) -> tuple[int, int, int]:
    value = (value or "").strip()
    if value.startswith("#") and len(value) == 7:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
    presets = {"white": (255, 255, 255), "black": (25, 25, 25), "red": (200, 40, 40)}
    return presets.get(value.lower(), (25, 25, 25))


def draw_sfx(page_image: Image.Image, sfx: Sfx, panel_box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = panel_box
    pw, ph = right - left, bottom - top
    fill = _parse_color(sfx.color)
    stroke = _parse_color(sfx.outline_color)
    tile = _render_sfx_tile(sfx, fill, stroke)
    if sfx.rotation:
        tile = tile.rotate(sfx.rotation, expand=True, resample=Image.BICUBIC)
    if sfx.box is not None:
        cx = left + int(sfx.box[0] * pw)
        cy = top + int(sfx.box[1] * ph)
    else:
        cx, cy = _anchor_point(sfx.position, panel_box)
    page_image.alpha_composite(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)))


def _render_sfx_tile(sfx: Sfx, fill: tuple[int, int, int], stroke: tuple[int, int, int]) -> Image.Image:
    font_path = find_dialogue_font_path()
    from PIL import ImageFont

    font = ImageFont.truetype(str(font_path), sfx.font_size) if font_path else ImageFont.load_default()
    pad = sfx.outline_width + 8
    if sfx.vertical:
        widths, heights = [], []
        for ch in sfx.text:
            bbox = font.getbbox(ch)
            widths.append(bbox[2] - bbox[0])
            heights.append(bbox[3] - bbox[1])
        tile_w = max(widths, default=sfx.font_size) + pad * 2
        tile_h = sum(int(sfx.font_size * 1.05) for _ in sfx.text) + pad * 2
        tile = Image.new("RGBA", (int(tile_w), int(tile_h)), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        y = pad
        for ch in sfx.text:
            td.text((tile_w / 2, y), ch, font=font, fill=(*fill, 255), anchor="ma",
                    stroke_width=sfx.outline_width, stroke_fill=(*stroke, 255))
            y += int(sfx.font_size * 1.05)
        return tile
    bbox = font.getbbox(sfx.text)
    tile_w = (bbox[2] - bbox[0]) + pad * 2
    tile_h = (bbox[3] - bbox[1]) + pad * 2
    tile = Image.new("RGBA", (int(tile_w), int(tile_h)), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.text((pad - bbox[0], pad - bbox[1]), sfx.text, font=font, fill=(*fill, 255),
            stroke_width=sfx.outline_width, stroke_fill=(*stroke, 255))
    return tile


def draw_page_number(draw: ImageDraw.ImageDraw, page_number: int, reading_direction: str) -> None:
    font = load_label_font(22)
    text = str(page_number)
    # 右綴じ(rtl)はノンブルを左下、左綴じは右下に置く。
    x = 40 if reading_direction == "rtl" else PAGE_SIZE[0] - 60
    draw.text((x, PAGE_SIZE[1] - 44), text, fill=(40, 40, 40, 255), font=font)


# --- 画像のはめ込み（パン・ズームcrop） ---

def fit_image_to_box(
    source: Image.Image,
    target_size: tuple[int, int],
    fit_mode: str,
    crop_anchor: str,
    scale: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    focal: tuple[float, float] | None = None,
) -> Image.Image:
    target_w, target_h = target_size
    if target_w <= 0 or target_h <= 0:
        return source
    source_w, source_h = source.size
    if source_w <= 0 or source_h <= 0:
        return Image.new("RGB", target_size, (230, 232, 235))

    if fit_mode == "contain":
        ratio = min(target_w / source_w, target_h / source_h)
        resized = source.resize((max(1, int(source_w * ratio)), max(1, int(source_h * ratio))))
        canvas = Image.new("RGB", target_size, (245, 245, 242))
        canvas.paste(resized, ((target_w - resized.width) // 2, (target_h - resized.height) // 2))
        return canvas

    base_ratio = max(target_w / source_w, target_h / source_h)
    ratio = base_ratio * max(1.0, scale)
    resized = source.resize((max(1, int(source_w * ratio)), max(1, int(source_h * ratio))))
    extra_x = max(0, resized.width - target_w)
    extra_y = max(0, resized.height - target_h)

    if focal is not None:
        left = int(focal[0] * resized.width - target_w / 2)
        top = int(focal[1] * resized.height - target_h / 2)
    else:
        frac_x = _anchor_fraction(crop_anchor, horizontal=True) + offset_x * 0.5
        frac_y = _anchor_fraction(crop_anchor, horizontal=False) + offset_y * 0.5
        left = int(extra_x * _clamp01(frac_x))
        top = int(extra_y * _clamp01(frac_y))
    left = max(0, min(left, extra_x))
    top = max(0, min(top, extra_y))
    return resized.crop((left, top, left + target_w, top + target_h))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _anchor_fraction(anchor: str, horizontal: bool) -> float:
    if horizontal:
        return {"left": 0.0, "right": 1.0}.get(anchor, 0.5)
    return {"top": 0.0, "bottom": 1.0}.get(anchor, 0.5)
