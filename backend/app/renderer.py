from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw, ImageFont

from .image_backends import load_font, wrap_text
from .schemas import Dialogue, MangaProject, Panel, Sfx

PAGE_SIZE = (1200, 1700)
PANEL_MARGIN = 8


def render_project_pages(project_id: str, manga: MangaProject, export_dir: Path) -> list[Path]:
    pages_dir = export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []
    for page in manga.pages:
        image = Image.new("RGB", PAGE_SIZE, (248, 248, 244))
        draw = ImageDraw.Draw(image)
        for panel in page.panels:
            draw_panel(image, draw, panel)
        draw_page_number(draw, page.page)
        target = pages_dir / f"page_{page.page:03d}.png"
        image.save(target)
        assets.append(target)
    return assets


def render_project_page(project_id: str, manga: MangaProject, page_number: int, export_dir: Path) -> Path:
    pages_dir = export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    page = next((item for item in manga.pages if item.page == page_number), None)
    if page is None:
        raise ValueError(f"ページが見つかりません: {page_number}")
    image = Image.new("RGB", PAGE_SIZE, (248, 248, 244))
    draw = ImageDraw.Draw(image)
    for panel in page.panels:
        draw_panel(image, draw, panel)
    draw_page_number(draw, page.page)
    target = pages_dir / f"page_{page.page:03d}.png"
    image.save(target)
    return target


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


def draw_panel(page_image: Image.Image, draw: ImageDraw.ImageDraw, panel: Panel) -> None:
    page_w, page_h = PAGE_SIZE
    left = int(panel.bbox[0] * page_w)
    top = int(panel.bbox[1] * page_h)
    width = int(panel.bbox[2] * page_w)
    height = int(panel.bbox[3] * page_h)
    box = (left, top, left + width, top + height)

    if panel.image_asset:
        asset_path = Path(panel.image_asset)
        if asset_path.exists():
            panel_image = Image.open(asset_path).convert("RGB")
            fitted = fit_image_to_box(panel_image, (width, height), panel.generation.fit_mode, panel.generation.crop_anchor)
            page_image.paste(fitted, (left, top))
        else:
            draw.rectangle(box, fill=(230, 232, 235))
    else:
        draw.rectangle(box, fill=(230, 232, 235))

    draw.rectangle(box, outline=(20, 20, 20), width=4)
    label_font = load_font(22)
    draw.text((left + 14, top + 10), panel.panel_id, fill=(20, 20, 20), font=label_font)

    for dialogue in panel.dialogue:
        draw_dialogue(draw, dialogue, box)
    for sfx in panel.sfx:
        draw_sfx(draw, sfx, box)


def draw_dialogue(draw: ImageDraw.ImageDraw, dialogue: Dialogue, panel_box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = panel_box
    panel_w = right - left
    panel_h = bottom - top
    if dialogue.box:
        x = left + int(dialogue.box[0] * panel_w)
        y = top + int(dialogue.box[1] * panel_h)
        bubble_w = int(dialogue.box[2] * panel_w)
        bubble_h = int(dialogue.box[3] * panel_h)
    else:
        bubble_w = max(220, min(430, panel_w - 32))
        bubble_h = 96
        positions = {
            "upper_left": (left + 22, top + 28),
            "upper_right": (right - bubble_w - 22, top + 28),
            "lower_left": (left + 22, bottom - bubble_h - 24),
            "lower_right": (right - bubble_w - 22, bottom - bubble_h - 24),
            "center": (left + (panel_w - bubble_w) // 2, top + (panel_h - bubble_h) // 2),
        }
        x, y = positions[dialogue.position]
    bubble_w = max(80, min(bubble_w, panel_w - 12))
    bubble_h = max(48, min(bubble_h, panel_h - 12))
    x = max(left + 6, min(x, right - bubble_w - 6))
    y = max(top + 6, min(y, bottom - bubble_h - 6))
    bubble_box = (x, y, x + bubble_w, y + bubble_h)
    outline_width = 5 if dialogue.balloon == "shout" else 3
    fill = (255, 255, 255) if dialogue.balloon != "thought" else (250, 250, 250)
    draw.rounded_rectangle(bubble_box, radius=28, fill=fill, outline=(25, 25, 25), width=outline_width)
    font, lines, line_height = fit_text(dialogue.text, bubble_w - 44, bubble_h - 28, dialogue.font_size, dialogue.max_lines)
    for index, line in enumerate(lines):
        draw.text((x + 22, y + 16 + index * line_height), line, fill=(20, 20, 20), font=font)


def draw_sfx(draw: ImageDraw.ImageDraw, sfx: Sfx, panel_box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = panel_box
    panel_w = right - left
    panel_h = bottom - top
    font = load_font(42)
    positions = {
        "upper_left": (left + 36, top + 42),
        "upper_right": (right - 180, top + 42),
        "lower_left": (left + 36, bottom - 90),
        "lower_right": (right - 180, bottom - 90),
        "center": (left + panel_w // 2 - 70, top + panel_h // 2 - 25),
    }
    x, y = positions[sfx.position]
    draw.text((x + 3, y + 3), sfx.text, fill=(255, 255, 255), font=font)
    draw.text((x, y), sfx.text, fill=(25, 25, 25), font=font)


def draw_page_number(draw: ImageDraw.ImageDraw, page_number: int) -> None:
    font = load_font(22)
    text = str(page_number)
    draw.text((PAGE_SIZE[0] - 60, PAGE_SIZE[1] - 44), text, fill=(40, 40, 40), font=font)


def fit_image_to_box(source: Image.Image, target_size: tuple[int, int], fit_mode: str, crop_anchor: str) -> Image.Image:
    target_w, target_h = target_size
    if target_w <= 0 or target_h <= 0:
        return source
    source_w, source_h = source.size
    if source_w <= 0 or source_h <= 0:
        return Image.new("RGB", target_size, (230, 232, 235))

    if fit_mode == "contain":
        scale = min(target_w / source_w, target_h / source_h)
        resized = source.resize((max(1, int(source_w * scale)), max(1, int(source_h * scale))))
        canvas = Image.new("RGB", target_size, (245, 245, 242))
        canvas.paste(resized, ((target_w - resized.width) // 2, (target_h - resized.height) // 2))
        return canvas

    scale = max(target_w / source_w, target_h / source_h)
    resized = source.resize((max(1, int(source_w * scale)), max(1, int(source_h * scale))))
    left = crop_offset(resized.width, target_w, crop_anchor, horizontal=True)
    top = crop_offset(resized.height, target_h, crop_anchor, horizontal=False)
    return resized.crop((left, top, left + target_w, top + target_h))


def crop_offset(source_length: int, target_length: int, anchor: str, horizontal: bool) -> int:
    extra = max(0, source_length - target_length)
    if horizontal and anchor == "left":
        return 0
    if horizontal and anchor == "right":
        return extra
    if not horizontal and anchor == "top":
        return 0
    if not horizontal and anchor == "bottom":
        return extra
    return extra // 2


def fit_text(text: str, max_width: int, max_height: int, preferred_size: int, max_lines: int) -> tuple[ImageFont.ImageFont, list[str], int]:
    for font_size in range(preferred_size, 9, -1):
        font = load_font(font_size)
        line_width = max(4, max_width // max(1, int(font_size * 0.62)))
        lines = wrap_text(text, line_width)[:max_lines]
        line_height = int(font_size * 1.22)
        if line_height * len(lines) <= max_height:
            return font, lines, line_height
    font = load_font(10)
    return font, wrap_text(text, max(4, max_width // 7))[:max_lines], 13
