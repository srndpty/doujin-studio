from __future__ import annotations

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


def export_cbz(project_id: str, page_assets: list[Path], export_dir: Path) -> Path:
    target = export_dir / project_id / f"{project_id}.cbz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for asset in sorted(page_assets):
            archive.write(asset, asset.name)
    return target


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
            panel_image = Image.open(asset_path).convert("RGB").resize((width, height))
            page_image.paste(panel_image, (left, top))
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
    bubble_box = (x, y, x + bubble_w, y + bubble_h)
    outline_width = 5 if dialogue.balloon == "shout" else 3
    fill = (255, 255, 255) if dialogue.balloon != "thought" else (250, 250, 250)
    draw.rounded_rectangle(bubble_box, radius=28, fill=fill, outline=(25, 25, 25), width=outline_width)
    font = load_font(24)
    for index, line in enumerate(wrap_text(dialogue.text, 14)[:3]):
        draw.text((x + 22, y + 16 + index * 28), line, fill=(20, 20, 20), font=font)


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
