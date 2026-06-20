from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw

from . import typeset
from .assets import resolve_asset_path
from .fonts import find_dialogue_font_path, load_label_font
from .schemas import Dialogue, MangaProject, Page, Panel, Sfx, TypographySettings

PAGE_SIZE = (1200, 1700)
PANEL_OUTLINE_WIDTH = 4
BUBBLE_INNER_PAD = 12
# 写植が成立する最小フォント（収まらない場合の床）。
TEXT_FLOOR_SIZE = 18


def render_project_pages(
    project_id: str, manga: MangaProject, export_dir: Path
) -> tuple[list[Path], list[str]]:
    pages_dir = export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []
    warnings: list[str] = []
    for page in manga.pages:
        target, page_warnings = _render_single_page(manga, page, pages_dir, export_dir)
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
    return _render_single_page(manga, page, pages_dir, export_dir)


def _render_single_page(
    manga: MangaProject, page: Page, pages_dir: Path, export_dir: Path
) -> tuple[Path, list[str]]:
    # レンダリング順: 背景 → 通常コマ → 背面overlay → 前面overlay → 吹き出し・SFX。
    image = Image.new("RGBA", PAGE_SIZE, (248, 248, 244, 255))
    draw = ImageDraw.Draw(image)
    warnings: list[str] = []
    panel_boxes = {panel.panel_id: _panel_box_px(panel) for panel in page.panels}

    for panel in page.panels:
        draw_panel_art(image, draw, panel, panel_boxes[panel.panel_id], export_dir)
    for overlay in sorted(page.overlay_elements, key=lambda item: item.z_index):
        if overlay.layer == "back":
            draw_overlay(image, draw, overlay, page, panel_boxes, export_dir)
    for overlay in sorted(page.overlay_elements, key=lambda item: item.z_index):
        if overlay.layer == "front":
            draw_overlay(image, draw, overlay, page, panel_boxes, export_dir)
    for panel in page.panels:
        warnings.extend(
            draw_panel_text(image, draw, panel, panel_boxes[panel.panel_id], manga.typography)
        )

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


def _panel_box_px(panel: Panel) -> tuple[int, int, int, int]:
    page_w, page_h = PAGE_SIZE
    left = int(panel.bbox[0] * page_w)
    top = int(panel.bbox[1] * page_h)
    width = int(panel.bbox[2] * page_w)
    height = int(panel.bbox[3] * page_h)
    return (left, top, left + width, top + height)


def _fit_panel_image(
    panel: Panel, box: tuple[int, int, int, int], export_dir: Path | None = None
) -> Image.Image | None:
    if not panel.image_asset:
        return None
    try:
        source_path = (
            resolve_asset_path(panel.image_asset, export_dir)
            if export_dir is not None
            else Path(panel.image_asset)
        )
    except ValueError:
        return None
    if not source_path.exists():
        return None
    width = box[2] - box[0]
    height = box[3] - box[1]
    source = Image.open(source_path).convert("RGB")
    return fit_image_to_box(
        source,
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


def draw_panel_art(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    box: tuple[int, int, int, int],
    export_dir: Path | None = None,
) -> None:
    """コマの画像と枠線・番号ラベルだけを描く（overlayより下のレイヤー）。"""
    fitted = _fit_panel_image(panel, box, export_dir)
    if fitted is not None:
        page_image.paste(fitted.convert("RGB"), (box[0], box[1]))
    else:
        draw.rectangle(box, fill=(230, 232, 235, 255))
    draw.rectangle(box, outline=(20, 20, 20, 255), width=PANEL_OUTLINE_WIDTH)


def draw_panel_text(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> list[str]:
    """吹き出し・SFXを描く（overlayより上のレイヤー）。"""
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


def draw_overlay(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    overlay,
    page,
    panel_boxes: dict[str, tuple[int, int, int, int]],
    export_dir: Path | None = None,
) -> None:
    """オーバーフレーム（コマ枠外の演出レイヤー）を合成する。"""
    page_w, page_h = PAGE_SIZE
    target_w = max(1, int(overlay.box[2] * page_w * overlay.scale))
    target_h = max(1, int(overlay.box[3] * page_h * overlay.scale))
    dest_x = int(overlay.box[0] * page_w)
    dest_y = int(overlay.box[1] * page_h)

    tile: Image.Image | None = None
    try:
        overlay_path = (
            resolve_asset_path(overlay.asset, export_dir)
            if overlay.asset and export_dir is not None
            else Path(overlay.asset)
            if overlay.asset
            else None
        )
    except ValueError:
        overlay_path = None
    if overlay_path and overlay_path.exists():
        tile = Image.open(overlay_path).convert("RGBA").resize((target_w, target_h))
        try:
            mask_path = (
                resolve_asset_path(overlay.mask_asset, export_dir)
                if overlay.mask_asset and export_dir is not None
                else Path(overlay.mask_asset)
                if overlay.mask_asset
                else None
            )
        except ValueError:
            mask_path = None
        if mask_path and mask_path.exists():
            mask = Image.open(mask_path).convert("L").resize((target_w, target_h))
            tile.putalpha(mask)
    if tile is None:
        # アセット未設定でも配置枠を点線で示す（後からマスク/抽出を接続できる）。
        draw.rectangle(
            (dest_x, dest_y, dest_x + target_w, dest_y + target_h),
            outline=(120, 120, 200, 255),
            width=3,
        )
        return
    if overlay.opacity < 1.0:
        alpha = tile.getchannel("A").point(lambda value: int(value * overlay.opacity))
        tile.putalpha(alpha)
    page_image.alpha_composite(tile, (dest_x, dest_y))

    # 指定コマの絵だけは手前に再描画し、overlayがそのコマに隠れるようにする。
    for panel_id in overlay.occluded_by_panel_ids:
        panel = next((item for item in page.panels if item.panel_id == panel_id), None)
        box = panel_boxes.get(panel_id)
        if panel is None or box is None:
            continue
        draw_panel_art(page_image, draw, panel, box, export_dir)


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


def _clamp_box(
    box: tuple[int, int, int, int], bounds: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    bl, bt, br, bb = bounds
    x0, y0, x1, y1 = box
    w = min(x1 - x0, br - bl)
    h = min(y1 - y0, bb - bt)
    x0 = max(bl, min(x0, br - w))
    y0 = max(bt, min(y0, bb - h))
    return (x0, y0, x0 + w, y0 + h)


def resolve_dialogue_layout(
    dialogue: Dialogue,
    panel_box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> tuple[tuple[int, int, int, int], typeset.TextLayout]:
    """吹き出し枠と写植レイアウトを決める（描画と検査で共通利用）。

    吹き出しはコマを埋め尽くさず内容に合わせて最小限の大きさにし、
    必要に応じてフォントを縮小する。明示的なboxがあればそれを尊重する。
    """
    left, top, right, bottom = panel_box
    pw, ph = right - left, bottom - top
    inset = PANEL_OUTLINE_WIDTH + 4
    bounds = (left + inset, top + inset, right - inset, bottom - inset)
    font_path = find_dialogue_font_path()
    font_path_str = str(font_path) if font_path else None
    vertical = dialogue.vertical
    pad = BUBBLE_INNER_PAD

    default_size = max(
        min(dialogue.font_size, typography.default_font_size or dialogue.font_size), TEXT_FLOOR_SIZE
    )
    # 形状ごとの余白係数（楕円/雲/爆発は内接ぶん広めに取る）。
    if dialogue.balloon in {"oval", "cloud", "burst"}:
        shape_x, shape_y = 1.24, 1.18
    else:
        shape_x, shape_y = 1.05, 1.05

    if dialogue.box:
        # 編集UIなどで指定された枠を尊重し、その中へ収める。
        bx = left + int(dialogue.box[0] * pw)
        by = top + int(dialogue.box[1] * ph)
        bubble = _clamp_box(
            (bx, by, bx + int(dialogue.box[2] * pw), by + int(dialogue.box[3] * ph)), bounds
        )
        inner_w = (bubble[2] - bubble[0]) / shape_x - pad * 2
        inner_h = (bubble[3] - bubble[1]) / shape_y - pad * 2
        layout = typeset.layout_text(
            dialogue.text,
            font_path_str,
            max(8, inner_w),
            max(8, inner_h),
            vertical,
            default_size,
            TEXT_FLOOR_SIZE,
        )
        return bubble, layout

    # 内容に合わせて自動サイズ。コマの一定割合を上限にする。
    if vertical:
        cap_w, cap_h = pw * 0.62, ph * 0.84
    else:
        cap_w, cap_h = pw * 0.86, ph * 0.58
    cap_w = min(cap_w, bounds[2] - bounds[0])
    cap_h = min(cap_h, bounds[3] - bounds[1])

    chosen: typeset.TextLayout | None = None
    for size in range(default_size, TEXT_FLOOR_SIZE - 1, -1):
        inner_w = cap_w / shape_x - pad * 2
        inner_h = cap_h / shape_y - pad * 2
        if inner_w < size or inner_h < size:
            continue
        layout = typeset.layout_text(
            dialogue.text, font_path_str, inner_w, inner_h, vertical, size, size
        )
        if layout.fits:
            chosen = layout
            break
    if chosen is None:
        inner_w = max(8.0, cap_w / shape_x - pad * 2)
        inner_h = max(8.0, cap_h / shape_y - pad * 2)
        chosen = typeset.layout_text(
            dialogue.text,
            font_path_str,
            inner_w,
            inner_h,
            vertical,
            TEXT_FLOOR_SIZE,
            TEXT_FLOOR_SIZE,
        )

    # 吹き出しを内容ぴったりに縮める。
    bubble_w = int(min(cap_w, chosen.width * shape_x + pad * 2))
    bubble_h = int(min(cap_h, chosen.height * shape_y + pad * 2))
    cx, cy = _anchor_point(dialogue.position, panel_box)
    bubble = _clamp_box(
        (cx - bubble_w // 2, cy - bubble_h // 2, cx + bubble_w // 2, cy + bubble_h // 2), bounds
    )
    return bubble, chosen


def draw_dialogue(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    dialogue: Dialogue,
    panel_box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> list[str]:
    bubble, layout = resolve_dialogue_layout(dialogue, panel_box, typography)
    font_path = find_dialogue_font_path()
    font_path_str = str(font_path) if font_path else None
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
        # ナレーションは四角枠。しっぽは付けない。
        draw.rectangle(bubble, fill=(252, 252, 250, 255), outline=outline, width=3)
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
    # 既定は吹き出し下方向へ短く出す（話者方向の代理）。
    bx = (bubble[0] + bubble[2]) / 2
    by = (bubble[1] + bubble[3]) / 2
    cx, cy = (left + pw / 2, bottom - ph * 0.1)
    dx, dy = cx - bx, cy - by
    norm = math.hypot(dx, dy) or 1.0
    reach = min(pw, ph) * 0.07
    return (
        int(bx + dx / norm * (abs(bubble[2] - bubble[0]) / 2 + reach)),
        int(by + dy / norm * (abs(bubble[3] - bubble[1]) / 2 + reach)),
    )


def _draw_tail(draw, dialogue, bubble, panel_box, outline, fill, square: bool = False) -> None:
    if dialogue.tail is not None and not dialogue.tail.enabled:
        return
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tip = _tail_tip(dialogue, bubble, panel_box)
    width_ratio = dialogue.tail.width if dialogue.tail is not None else 0.12
    base_w = max(12, min((x1 - x0) * width_ratio, 26))
    # 付け根は吹き出しの縁に取る。
    dx, dy = tip[0] - cx, tip[1] - cy
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
    base_cx = cx + ux * rx * 0.98
    base_cy = cy + uy * ry * 0.98
    # 縁に沿う垂直方向ベクトル。
    px, py = -uy, ux
    p1 = (base_cx + px * base_w / 2, base_cy + py * base_w / 2)
    p2 = (base_cx - px * base_w / 2, base_cy - py * base_w / 2)
    # 付け根（吹き出し内側）の線は描かず、斜辺だけ縁取りして自然に繋げる。
    draw.polygon([p1, p2, tip], fill=fill)
    draw.line([p1, tip], fill=outline, width=3)
    draw.line([p2, tip], fill=outline, width=3)


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
    draw.rounded_rectangle(
        (x0 + w * 0.06, y0 + h * 0.12, x1 - w * 0.06, y1 - h * 0.12),
        radius=int(min(w, h) * 0.3),
        fill=fill,
    )
    # 縁にこぶを並べて雲状にする。
    bumps = max(6, int(w / 60))
    rb = min(w, h) * 0.12
    for i in range(bumps):
        t = i / bumps
        for cxb, cyb in (
            (x0 + w * t, y0 + h * 0.12),
            (x0 + w * t, y1 - h * 0.12),
        ):
            draw.ellipse(
                (cxb - rb, cyb - rb, cxb + rb, cyb + rb), fill=fill, outline=outline, width=2
            )
    for i in range(max(3, int(h / 60))):
        t = i / max(3, int(h / 60))
        for cxb, cyb in (
            (x0 + w * 0.06, y0 + h * t),
            (x1 - w * 0.06, y0 + h * t),
        ):
            draw.ellipse(
                (cxb - rb, cyb - rb, cxb + rb, cyb + rb), fill=fill, outline=outline, width=2
            )


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


def _render_sfx_tile(
    sfx: Sfx, fill: tuple[int, int, int], stroke: tuple[int, int, int]
) -> Image.Image:
    font_path = find_dialogue_font_path()
    from PIL import ImageFont

    font = (
        ImageFont.truetype(str(font_path), sfx.font_size) if font_path else ImageFont.load_default()
    )
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
            td.text(
                (tile_w / 2, y),
                ch,
                font=font,
                fill=(*fill, 255),
                anchor="ma",
                stroke_width=sfx.outline_width,
                stroke_fill=(*stroke, 255),
            )
            y += int(sfx.font_size * 1.05)
        return tile
    bbox = font.getbbox(sfx.text)
    tile_w = (bbox[2] - bbox[0]) + pad * 2
    tile_h = (bbox[3] - bbox[1]) + pad * 2
    tile = Image.new("RGBA", (int(tile_w), int(tile_h)), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.text(
        (pad - bbox[0], pad - bbox[1]),
        sfx.text,
        font=font,
        fill=(*fill, 255),
        stroke_width=sfx.outline_width,
        stroke_fill=(*stroke, 255),
    )
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
