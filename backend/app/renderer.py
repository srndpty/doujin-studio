from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw

from . import typeset
from .assets import resolve_asset_path
from .fonts import find_dialogue_font_path, find_sfx_font_path, load_label_font
from .schemas import Dialogue, MangaProject, Page, Panel, Sfx, TypographySettings

PAGE_SIZE = (1200, 1700)
PANEL_OUTLINE_WIDTH = 4
BUBBLE_INNER_PAD = 12
# 写植が成立する最小フォント（収まらない場合の床）。
TEXT_FLOOR_SIZE = 18

# 吹き出し形状ごとの「内接テキスト矩形→外形」係数(fx, fy)。
# bubble = text_block * f + 2*pad で外形を決め、同じtext_areaへ描画する。
# 楕円/破裂形は矩形を内接させるため√2以上を取り、はみ出しを防ぐ（領域3）。
SHAPE_INSCRIBE: dict[str, tuple[float, float]] = {
    "oval": (1.45, 1.45),
    "burst": (1.95, 1.95),
    "cloud": (1.30, 1.55),
    "caption": (1.04, 1.04),
    "none": (1.02, 1.02),
}


def _refresh_sfx_font_cache() -> None:
    """擬音フォントの探索キャッシュを破棄する。

    利用者が登録ディレクトリ(~/.doujin-studio/fonts/sfx)へフォントを追加した直後でも、
    再描画で反映されるよう、描画の入口でキャッシュを更新する。
    """
    find_sfx_font_path.cache_clear()


def render_project_pages(
    project_id: str,
    manga: MangaProject,
    export_dir: Path,
    *,
    output_dir: Path | None = None,
) -> tuple[list[Path], list[str]]:
    _refresh_sfx_font_cache()
    pages_dir = output_dir or export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []
    warnings: list[str] = []
    for page in manga.pages:
        target, page_warnings = _render_single_page(manga, page, pages_dir, export_dir)
        assets.append(target)
        warnings.extend(page_warnings)
    return assets, warnings


def render_project_page(
    project_id: str,
    manga: MangaProject,
    page_number: int,
    export_dir: Path,
    *,
    output_dir: Path | None = None,
) -> tuple[Path, list[str]]:
    _refresh_sfx_font_cache()
    pages_dir = output_dir or export_dir / project_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    page = next((item for item in manga.pages if item.page == page_number), None)
    if page is None:
        raise ValueError(f"ページが見つかりません: {page_number}")
    return _render_single_page(manga, page, pages_dir, export_dir)


def _render_single_page(
    manga: MangaProject, page: Page, pages_dir: Path, export_dir: Path
) -> tuple[Path, list[str]]:
    # レンダリング順: 背景 → 背面overlay → パネル単位(z順: art→below SFX→dialogue→above SFX)
    # → 前面overlay。PageEditorのプレビューと同じ契約にし、背面大ゴマの文字が手前コマを
    # 貫通しないようにする。
    image = Image.new("RGBA", PAGE_SIZE, (248, 248, 244, 255))
    draw = ImageDraw.Draw(image)
    warnings: list[str] = []
    panel_boxes = {panel.panel_id: _panel_box_px(panel) for panel in page.panels}

    for overlay in sorted(page.overlay_elements, key=lambda item: item.z_index):
        if overlay.layer == "back":
            draw_overlay(image, draw, overlay, page, panel_boxes, export_dir)
    for panel in sorted(page.panels, key=lambda item: item.z_index):
        draw_panel_art(image, draw, panel, panel_boxes[panel.panel_id], export_dir)
        warnings.extend(
            draw_panel_text(image, draw, panel, panel_boxes[panel.panel_id], manga.typography)
        )
    for overlay in sorted(page.overlay_elements, key=lambda item: item.z_index):
        if overlay.layer == "front":
            draw_overlay(image, draw, overlay, page, panel_boxes, export_dir)

    draw_page_number(draw, page.page, manga.reading_direction)
    target = pages_dir / f"page_{page.page:03d}.png"
    image.convert("RGB").save(target)
    return target, [f"{page.page}ページ {message}" for message in warnings]


def export_cbz(
    project_id: str,
    title: str,
    page_assets: list[Path],
    export_dir: Path,
    *,
    output_dir: Path | None = None,
) -> Path:
    safe_title = sanitize_export_filename(title)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = (output_dir or export_dir / project_id) / f"{safe_title}-{timestamp}.cbz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for index, asset in enumerate(sorted(page_assets), start=1):
            # 内部assetは入力hash付き不変名だが、CBZ内は閲覧ソフト向けの連番名にする。
            archive.write(asset, f"page_{index:03d}.png")
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


def panel_frame_points(panel: Panel) -> list[tuple[float, float]]:
    """コマ枠のページ座標ポリゴン（正本）。

    frame_pointsがあればそれを使い、無ければbbox（+bbox相対shape_points）から導出する。
    新しい描画・編集表示はこの値を正とし、bboxは外接矩形・互換用に残す（領域: レイアウト）。
    """
    if panel.frame_points:
        return [(x, y) for x, y in panel.frame_points]
    x, y, w, h = panel.bbox
    if panel.shape_points:
        return [(x + sx * w, y + sy * h) for sx, sy in panel.shape_points]
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def panel_frame_points_px(panel: Panel) -> list[tuple[int, int]]:
    """コマ枠ポリゴンをページピクセル座標へ写す。"""
    page_w, page_h = PAGE_SIZE
    return [(round(x * page_w), round(y * page_h)) for x, y in panel_frame_points(panel)]


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
    """コマの画像と枠線・番号ラベルだけを描く（overlayより下のレイヤー）。

    frame_points（ページ座標ポリゴン）があれば正本として用い、無ければbbox（+bbox相対
    shape_points）から導出した形で描く。重なり順はz_indexで制御する（呼び出し側でsort）。
    """
    if panel.frame_points or panel.shape_points:
        polygon = panel_frame_points_px(panel)
        xs = [px for px, _ in polygon]
        ys = [py for _, py in polygon]
        fit_box = (min(xs), min(ys), max(xs), max(ys))
        _paste_polygon_panel(page_image, draw, panel, fit_box, polygon, export_dir)
        return
    fitted = _fit_panel_image(panel, box, export_dir)
    width, height = box[2] - box[0], box[3] - box[1]
    tile = (
        fitted.convert("RGBA")
        if fitted is not None
        else Image.new("RGBA", (width, height), (230, 232, 235, 255))
    )
    if panel.shape_points:
        relative_points = [(int(x * width), int(y * height)) for x, y in panel.shape_points]
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).polygon(relative_points, fill=255)
        page_image.paste(tile, (box[0], box[1]), mask)
        absolute_points = [(box[0] + x, box[1] + y) for x, y in relative_points]
        draw.line(
            absolute_points + [absolute_points[0]],
            fill=(20, 20, 20, 255),
            width=PANEL_OUTLINE_WIDTH,
            joint="curve",
        )
    else:
        page_image.paste(tile, (box[0], box[1]))
        draw.rectangle(box, outline=(20, 20, 20, 255), width=PANEL_OUTLINE_WIDTH)


def _paste_polygon_panel(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    fit_box: tuple[int, int, int, int],
    polygon: list[tuple[int, int]],
    export_dir: Path | None,
) -> None:
    """ページ座標ポリゴンのコマ枠に、外接矩形へfitした画像をマスクして貼る。"""
    width, height = fit_box[2] - fit_box[0], fit_box[3] - fit_box[1]
    if width <= 0 or height <= 0:
        return
    fitted = _fit_panel_image(panel, fit_box, export_dir)
    tile = (
        fitted.convert("RGBA")
        if fitted is not None
        else Image.new("RGBA", (width, height), (230, 232, 235, 255))
    )
    relative_points = [(px - fit_box[0], py - fit_box[1]) for px, py in polygon]
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).polygon(relative_points, fill=255)
    page_image.paste(tile, (fit_box[0], fit_box[1]), mask)
    draw.line(
        polygon + [polygon[0]],
        fill=(20, 20, 20, 255),
        width=PANEL_OUTLINE_WIDTH,
        joint="curve",
    )


def draw_panel_text(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> list[str]:
    """吹き出し・SFXを描く（overlayより上のレイヤー）。"""
    warnings: list[str] = []
    # 話者の人物領域（character_layout）を引き、しっぽの向きの基準にする。
    speaker_layout = {entry.id: entry for entry in panel.character_layout}
    for sfx in panel.sfx:
        if sfx.layer == "below":
            draw_sfx(page_image, sfx, box)
    for dialogue in panel.dialogue:
        entry = speaker_layout.get(dialogue.speaker) if dialogue.on_screen else None
        speaker_target = _speaker_target_point(entry, box) if entry is not None else None
        warnings.extend(draw_dialogue(page_image, draw, dialogue, box, typography, speaker_target))
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


def _speaker_target_point(entry, panel_box: tuple[int, int, int, int]) -> tuple[int, int]:
    """話者のしっぽ照準点。region_boxがあればその中心、無ければ立ち位置アンカーを使う。"""
    if entry.region_box is not None:
        left, top, right, bottom = panel_box
        pw, ph = right - left, bottom - top
        rx, ry, rw, rh = entry.region_box
        return (int(left + (rx + rw / 2) * pw), int(top + (ry + rh / 2) * ph))
    return _anchor_point(entry.position, panel_box)


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


@dataclass
class BubbleLayout:
    """吹き出し外形・内接テキスト矩形・写植レイアウトを一括で保持する。

    収まり判定(layout.fits)と実際の描画(text_area)を同じ計算結果から導き、
    「判定は通るのに描画でははみ出す」という不一致を防ぐ（領域3）。
    """

    bubble: tuple[int, int, int, int]
    text_area: tuple[int, int, int, int]
    layout: typeset.TextLayout


def _centered_area(
    bubble: tuple[int, int, int, int], block_w: float, block_h: float
) -> tuple[int, int, int, int]:
    """吹き出し中心に、テキストブロック(block_w×block_h)ちょうどの矩形を取る。"""
    bx0, by0, bx1, by1 = bubble
    cx = (bx0 + bx1) / 2
    cy = (by0 + by1) / 2
    return (
        int(cx - block_w / 2),
        int(cy - block_h / 2),
        int(cx + block_w / 2),
        int(cy + block_h / 2),
    )


def compute_bubble_layout(
    dialogue: Dialogue,
    panel_box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> BubbleLayout:
    """吹き出し外形・内接テキスト矩形・写植を決める（描画と検査で共通利用）。

    収まらない場合は「吹き出し拡張 → フォント縮小」の順で全文保持を試み、
    最小サイズでも収まらなければ layout.fits=False を返す（出力前エラーの根拠）。
    """
    left, top, right, bottom = panel_box
    pw, ph = right - left, bottom - top
    inset = PANEL_OUTLINE_WIDTH + 4
    bounds = (left + inset, top + inset, right - inset, bottom - inset)
    bounds_w = max(8, bounds[2] - bounds[0])
    bounds_h = max(8, bounds[3] - bounds[1])
    font_path = find_dialogue_font_path(typography.primary_font)
    font_path_str = str(font_path) if font_path else None
    vertical = dialogue.vertical
    pad = BUBBLE_INNER_PAD

    default_size = max(dialogue.font_size or typography.default_font_size, TEXT_FLOOR_SIZE)
    min_size = max(dialogue.min_font_size or typography.min_font_size, TEXT_FLOOR_SIZE)
    min_size = min(min_size, default_size)
    fx, fy = SHAPE_INSCRIBE.get(dialogue.balloon, (1.05, 1.05))

    def fit_in(cap_w: float, cap_h: float) -> typeset.TextLayout | None:
        for size in range(default_size, min_size - 1, -1):
            inner_w = (cap_w - pad * 2) / fx
            inner_h = (cap_h - pad * 2) / fy
            if inner_w < size or inner_h < size:
                continue
            layout = typeset.layout_text(
                dialogue.text,
                font_path_str,
                inner_w,
                inner_h,
                vertical,
                size,
                size,
                dialogue.max_lines,
            )
            if layout.fits:
                return layout
        return None

    if dialogue.box:
        # 編集UIなどで指定された枠を尊重し、その中へ収める。
        bx = left + int(dialogue.box[0] * pw)
        by = top + int(dialogue.box[1] * ph)
        bubble = _clamp_box(
            (bx, by, bx + int(dialogue.box[2] * pw), by + int(dialogue.box[3] * ph)), bounds
        )
        inner_w = max(8.0, (bubble[2] - bubble[0] - pad * 2) / fx)
        inner_h = max(8.0, (bubble[3] - bubble[1] - pad * 2) / fy)
        layout = typeset.layout_text(
            dialogue.text,
            font_path_str,
            inner_w,
            inner_h,
            vertical,
            default_size,
            min_size,
            dialogue.max_lines,
        )
        return BubbleLayout(bubble, _centered_area(bubble, layout.width, layout.height), layout)

    # 自動サイズ。基準上限→コマ全域の順に「拡張」しながら最大フォントで収める。
    if vertical:
        base_w, base_h = pw * 0.62, ph * 0.84
    else:
        base_w, base_h = pw * 0.86, ph * 0.58
    caps = [
        (min(base_w, bounds_w), min(base_h, bounds_h)),
        (float(bounds_w), float(bounds_h)),
    ]
    chosen: typeset.TextLayout | None = None
    chosen_cap = caps[-1]
    for cap_w, cap_h in caps:
        fitted = fit_in(cap_w, cap_h)
        if fitted is not None:
            chosen = fitted
            chosen_cap = (cap_w, cap_h)
            break
    if chosen is None:
        cap_w, cap_h = caps[-1]
        inner_w = max(8.0, (cap_w - pad * 2) / fx)
        inner_h = max(8.0, (cap_h - pad * 2) / fy)
        chosen = typeset.layout_text(
            dialogue.text,
            font_path_str,
            inner_w,
            inner_h,
            vertical,
            min_size,
            min_size,
            dialogue.max_lines,
        )
        chosen_cap = (cap_w, cap_h)

    cap_w, cap_h = chosen_cap
    bubble_w = int(min(cap_w, chosen.width * fx + pad * 2))
    bubble_h = int(min(cap_h, chosen.height * fy + pad * 2))
    cx, cy = _anchor_point(dialogue.position, panel_box)
    bubble = _clamp_box(
        (cx - bubble_w // 2, cy - bubble_h // 2, cx + bubble_w // 2, cy + bubble_h // 2), bounds
    )
    return BubbleLayout(bubble, _centered_area(bubble, chosen.width, chosen.height), chosen)


def resolve_dialogue_layout(
    dialogue: Dialogue,
    panel_box: tuple[int, int, int, int],
    typography: TypographySettings,
) -> tuple[tuple[int, int, int, int], typeset.TextLayout]:
    """後方互換: 吹き出し外形と写植レイアウトのタプルを返す。"""
    result = compute_bubble_layout(dialogue, panel_box, typography)
    return result.bubble, result.layout


def draw_dialogue(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    dialogue: Dialogue,
    panel_box: tuple[int, int, int, int],
    typography: TypographySettings,
    speaker_target: tuple[int, int] | None = None,
) -> list[str]:
    resolved = compute_bubble_layout(dialogue, panel_box, typography)
    bubble, area, layout = resolved.bubble, resolved.text_area, resolved.layout
    # 写植フォントもtypography.primary_fontで解決する（レイアウトと描画を一致させる）。
    font_path = find_dialogue_font_path(typography.primary_font)
    font_path_str = str(font_path) if font_path else None
    warnings = list(layout.warnings)
    fill = (20, 20, 20)
    stroke_width = 0
    stroke_fill = (255, 255, 255)

    if dialogue.balloon == "none":
        stroke_width = max(3, int(layout.font_size * 0.12))
    else:
        _draw_balloon_shape(page_image, draw, dialogue, bubble, panel_box, speaker_target)

    typeset.draw_layout(page_image, layout, font_path_str, area, fill, stroke_width, stroke_fill)
    return [f"{dialogue.speaker}: {message}" for message in warnings]


def dialogue_draws_tail(dialogue: Dialogue) -> bool:
    """この台詞に吹き出しのしっぽを描くか。

    画面外台詞(on_screen=False)はしっぽを出さない。tailが明示的に無効化されていれば
    出さない。on_screenを描画時に評価するため、再編集で画面内へ戻せばしっぽが復活する。
    """
    if not dialogue.on_screen:
        return False
    if dialogue.tail is not None and not dialogue.tail.enabled:
        return False
    return True


# 吹き出し外形の拡大率。PILは図形にアンチエイリアスを掛けないため、拡大タイルへ描いて
# LANCZOS縮小し、縁のジャギーを滑らかにする。文字は等倍で別途描くので鮮明さは保つ。
BALLOON_SUPERSAMPLE = 3


def _shift_scale(
    box: tuple[float, float, float, float], ox: float, oy: float, scale: float
) -> tuple[int, int, int, int]:
    """ページ座標のboxを、(ox,oy)原点・scale倍のローカルタイル座標へ写す。"""
    return (
        round((box[0] - ox) * scale),
        round((box[1] - oy) * scale),
        round((box[2] - ox) * scale),
        round((box[3] - oy) * scale),
    )


def _draw_balloon_shape(
    page_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    dialogue: Dialogue,
    bubble: tuple[int, int, int, int],
    panel_box: tuple[int, int, int, int],
    speaker_target: tuple[int, int] | None = None,
) -> None:
    page_w, page_h = PAGE_SIZE
    show_tail = dialogue.balloon != "caption" and dialogue_draws_tail(dialogue)
    xs = [bubble[0], bubble[2]]
    ys = [bubble[1], bubble[3]]
    if show_tail:
        tip = _tail_tip(dialogue, bubble, panel_box, speaker_target)
        xs.append(tip[0])
        ys.append(tip[1])
    # しっぽ先端まで含む描画範囲をとり、その分だけ拡大タイルを確保する。
    margin = max(8, int((bubble[2] - bubble[0]) * 0.15))
    rx0 = max(0, int(min(xs)) - margin)
    ry0 = max(0, int(min(ys)) - margin)
    rx1 = min(page_w, int(max(xs)) + margin)
    ry1 = min(page_h, int(max(ys)) + margin)
    if rx1 <= rx0 or ry1 <= ry0:
        return
    s = BALLOON_SUPERSAMPLE
    tile = Image.new("RGBA", ((rx1 - rx0) * s, (ry1 - ry0) * s), (0, 0, 0, 0))
    tile_draw = ImageDraw.Draw(tile)
    bubble_s = _shift_scale(bubble, rx0, ry0, s)
    panel_s = _shift_scale(panel_box, rx0, ry0, s)
    # speaker_targetはページpx。タイル（原点ずらし・scale倍）座標へ写してから渡す。
    target_s = (
        (round((speaker_target[0] - rx0) * s), round((speaker_target[1] - ry0) * s))
        if speaker_target is not None
        else None
    )
    _paint_balloon(tile_draw, dialogue, bubble_s, panel_s, show_tail, target_s, s)
    smoothed = tile.resize((rx1 - rx0, ry1 - ry0), Image.LANCZOS)
    page_image.alpha_composite(smoothed, (rx0, ry0))


def _paint_balloon(
    draw: ImageDraw.ImageDraw,
    dialogue: Dialogue,
    bubble: tuple[int, int, int, int],
    panel_box: tuple[int, int, int, int],
    show_tail: bool,
    speaker_target: tuple[int, int] | None,
    scale: float,
) -> None:
    """拡大タイルへ吹き出し外形を描く。線幅・個数はscaleに合わせる。"""
    outline = (25, 25, 25, 255)
    white = (255, 255, 255, 255)
    width3 = max(1, round(3 * scale))
    if dialogue.balloon == "caption":
        draw.rectangle(bubble, fill=(252, 252, 250, 255), outline=outline, width=width3)
        return
    if dialogue.balloon == "burst":
        _draw_burst(draw, bubble, outline, white, scale)
        if show_tail:
            _draw_tail(
                draw,
                dialogue,
                bubble,
                panel_box,
                outline,
                white,
                speaker_target=speaker_target,
                scale=scale,
            )
        return
    if dialogue.balloon == "cloud":
        _draw_cloud(draw, bubble, outline, white, scale)
        if show_tail:
            _draw_cloud_tail(
                draw, dialogue, bubble, panel_box, outline, white, speaker_target, scale
            )
        return
    # oval（標準の楕円）
    draw.ellipse(bubble, fill=white, outline=outline, width=width3)
    if show_tail:
        _draw_tail(
            draw,
            dialogue,
            bubble,
            panel_box,
            outline,
            white,
            speaker_target=speaker_target,
            scale=scale,
        )


def _tail_tip(dialogue: Dialogue, bubble, panel_box, speaker_target: tuple[int, int] | None = None):
    left, top, right, bottom = panel_box
    pw, ph = right - left, bottom - top
    if dialogue.tail is not None:
        return (left + int(dialogue.tail.tip[0] * pw), top + int(dialogue.tail.tip[1] * ph))
    bx = (bubble[0] + bubble[2]) / 2
    by = (bubble[1] + bubble[3]) / 2
    if speaker_target is not None:
        # 話者の人物領域中心（無ければ立ち位置）へしっぽを向ける。
        cx, cy = speaker_target
    else:
        # 既定は吹き出し下方向へ短く出す（話者方向の代理）。
        cx, cy = (left + pw / 2, bottom - ph * 0.1)
    dx, dy = cx - bx, cy - by
    norm = math.hypot(dx, dy) or 1.0
    reach = min(pw, ph) * 0.07
    return (
        int(bx + dx / norm * (abs(bubble[2] - bubble[0]) / 2 + reach)),
        int(by + dy / norm * (abs(bubble[3] - bubble[1]) / 2 + reach)),
    )


def _draw_tail(
    draw,
    dialogue,
    bubble,
    panel_box,
    outline,
    fill,
    square: bool = False,
    speaker_target=None,
    scale: float = 1.0,
) -> None:
    if dialogue.tail is not None and not dialogue.tail.enabled:
        return
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tip = _tail_tip(dialogue, bubble, panel_box, speaker_target)
    width_ratio = dialogue.tail.width if dialogue.tail is not None else 0.12
    base_w = max(12 * scale, min((x1 - x0) * width_ratio, 26 * scale))
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
    width3 = max(1, round(3 * scale))
    draw.polygon([p1, p2, tip], fill=fill)
    draw.line([p1, tip], fill=outline, width=width3)
    draw.line([p2, tip], fill=outline, width=width3)


def _draw_cloud_tail(
    draw, dialogue, bubble, panel_box, outline, fill, speaker_target=None, scale: float = 1.0
) -> None:
    if dialogue.tail is not None and not dialogue.tail.enabled:
        return
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tip = _tail_tip(dialogue, bubble, panel_box, speaker_target)
    width2 = max(1, round(2 * scale))
    for i, t in enumerate((0.45, 0.72, 0.95)):
        bx = cx + (tip[0] - cx) * t
        by = cy + (tip[1] - cy) * t
        r = max(round(4 * scale), int((x1 - x0) * 0.05 * (1 - i * 0.28)))
        draw.ellipse((bx - r, by - r, bx + r, by + r), fill=fill, outline=outline, width=width2)


def _draw_cloud(draw, bubble, outline, fill, scale: float = 1.0) -> None:
    x0, y0, x1, y1 = bubble
    w, h = x1 - x0, y1 - y0
    width2 = max(1, round(2 * scale))
    draw.rounded_rectangle(
        (x0 + w * 0.06, y0 + h * 0.12, x1 - w * 0.06, y1 - h * 0.12),
        radius=int(min(w, h) * 0.3),
        fill=fill,
    )
    # 縁にこぶを並べて雲状にする。個数はscaleを除いた実寸で決め、拡大しても数を増やさない。
    bumps = max(6, int(w / (60 * scale)))
    rb = min(w, h) * 0.12
    for i in range(bumps):
        t = i / bumps
        for cxb, cyb in (
            (x0 + w * t, y0 + h * 0.12),
            (x0 + w * t, y1 - h * 0.12),
        ):
            draw.ellipse(
                (cxb - rb, cyb - rb, cxb + rb, cyb + rb), fill=fill, outline=outline, width=width2
            )
    vbumps = max(3, int(h / (60 * scale)))
    for i in range(vbumps):
        t = i / vbumps
        for cxb, cyb in (
            (x0 + w * 0.06, y0 + h * t),
            (x1 - w * 0.06, y0 + h * t),
        ):
            draw.ellipse(
                (cxb - rb, cyb - rb, cxb + rb, cyb + rb), fill=fill, outline=outline, width=width2
            )


def _draw_burst(draw, bubble, outline, fill, scale: float = 1.0) -> None:
    x0, y0, x1, y1 = bubble
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
    spikes = 16
    points = []
    for i in range(spikes * 2):
        angle = math.pi * i / spikes
        radius = 1.0 if i % 2 == 0 else 0.74
        points.append((cx + math.cos(angle) * rx * radius, cy + math.sin(angle) * ry * radius))
    draw.polygon(points, fill=fill, outline=outline, width=max(1, round(2 * scale)))


# --- SFX ---


def _parse_color(value: str) -> tuple[int, int, int]:
    value = (value or "").strip()
    if value.startswith("#") and len(value) == 7:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
    presets = {"white": (255, 255, 255), "black": (25, 25, 25), "red": (200, 40, 40)}
    return presets.get(value.lower(), (25, 25, 25))


@dataclass(frozen=True)
class SfxStyleParams:
    """擬音styleごとの描画パラメータ（文字ごとのゆらぎ量と縁取り補正）。"""

    jitter_pos: float  # 位置ゆらぎ（フォントサイズ比）
    jitter_rot: float  # 文字ごとの回転（度）
    jitter_scale: float  # 拡縮ゆらぎ（±割合）
    outline_boost: int  # styleが上乗せする縁取り幅


# 手描き=ゆらぎ大、impact=拡縮と太縁、quiet=ほぼ整列の小さめ。
SFX_STYLE_PRESETS: dict[str, SfxStyleParams] = {
    "handwritten": SfxStyleParams(
        jitter_pos=0.12, jitter_rot=8.0, jitter_scale=0.14, outline_boost=0
    ),
    "small_handwritten": SfxStyleParams(
        jitter_pos=0.07, jitter_rot=5.0, jitter_scale=0.08, outline_boost=0
    ),
    "impact": SfxStyleParams(jitter_pos=0.05, jitter_rot=3.0, jitter_scale=0.22, outline_boost=3),
    "quiet": SfxStyleParams(jitter_pos=0.02, jitter_rot=1.5, jitter_scale=0.04, outline_boost=0),
}
_DEFAULT_SFX_STYLE = SFX_STYLE_PRESETS["small_handwritten"]


def sfx_style_params(style: str) -> SfxStyleParams:
    return SFX_STYLE_PRESETS.get(style, _DEFAULT_SFX_STYLE)


def _glyph_jitter(
    seed_key: str, index: int, ch: str, params: SfxStyleParams
) -> tuple[float, float, float, float]:
    """文字ごとの(回転, 拡縮, dx, dy)を決定的に求める。

    同じ(style, text, 位置, 文字)なら必ず同じ値になり、再描画で揺れない（領域4）。
    """
    digest = hashlib.md5(f"{seed_key}|{index}|{ch}".encode("utf-8")).digest()

    # 4バイトずつ[0,1)へ正規化して符号付きゆらぎへ変換する。
    def unit(offset: int) -> float:
        value = int.from_bytes(digest[offset : offset + 4], "big") / 0xFFFFFFFF
        return value * 2.0 - 1.0

    rot = unit(0) * params.jitter_rot
    scale = 1.0 + unit(4) * params.jitter_scale
    dx = unit(8) * params.jitter_pos
    dy = unit(12) * params.jitter_pos
    return rot, max(0.4, scale), dx, dy


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


def sfx_bbox_px(sfx: Sfx, panel_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """擬音の配置矩形（ページpx）を返す。プリフライトの衝突検出と共通利用する。"""
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
    x0 = int(cx - tile.width / 2)
    y0 = int(cy - tile.height / 2)
    return (x0, y0, x0 + tile.width, y0 + tile.height)


def _make_glyph_tile(
    font, ch: str, fill: tuple[int, int, int], stroke: tuple[int, int, int], outline_width: int
) -> Image.Image:
    pad = outline_width + 6
    try:
        bbox = font.getbbox(ch)
    except Exception:
        bbox = (0, 0, len(ch) * 10, 16)
    w = max(1, bbox[2] - bbox[0]) + pad * 2
    h = max(1, bbox[3] - bbox[1]) + pad * 2
    tile = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (pad - bbox[0], pad - bbox[1]),
        ch,
        font=font,
        fill=(*fill, 255),
        stroke_width=outline_width,
        stroke_fill=(*stroke, 255),
    )
    return tile


def _render_sfx_tile(
    sfx: Sfx, fill: tuple[int, int, int], stroke: tuple[int, int, int]
) -> Image.Image:
    """styleに応じて文字ごとにゆらぎを与えた擬音タイルを作る。

    手描き風では各文字へ決定的な傾き・拡縮・ずれを加える。styleが整列系(quiet)なら
    ゆらぎはごく小さくなる。改行は区切りとして保持し、縦書きなら列、横書きなら行を
    分ける。フォントは擬音用→台詞用の順で解決する。
    """
    from PIL import ImageFont

    font_path = find_sfx_font_path()
    font = (
        ImageFont.truetype(str(font_path), sfx.font_size) if font_path else ImageFont.load_default()
    )
    params = sfx_style_params(sfx.style)
    outline_width = sfx.outline_width + params.outline_boost
    # 改行を区切りとして保持する（縦書き=列、横書き=行）。空セグメントは無視する。
    segments = [list(segment) for segment in sfx.text.split("\n")]
    segments = [segment for segment in segments if segment]
    if not segments:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))

    # seedに位置・box・縦横も含め、同じ文字列を別位置へ置いてもゆらぎが複製されないようにする。
    seed_key = f"{sfx.style}|{sfx.text}|{sfx.position}|{sfx.box}|{sfx.vertical}"
    cell = sfx.font_size
    advance = int(cell * 1.05)
    band = int(cell * 1.3)  # 列幅(縦書き)／行高(横書き)
    # ゆらぎや拡縮を受け止める余白。
    margin = int(cell * (0.9 + params.jitter_scale + params.jitter_pos))
    longest = max(len(segment) for segment in segments)
    line_count = len(segments)
    if sfx.vertical:
        canvas_w = band * line_count + margin * 2
        canvas_h = advance * longest + margin * 2
    else:
        canvas_w = advance * longest + margin * 2
        canvas_h = band * line_count + margin * 2
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # 文字インデックスはセグメントを跨いで連番にし、ゆらぎの決定性を保つ。
    glyph_index = 0
    for line_index, segment in enumerate(segments):
        for char_index, ch in enumerate(segment):
            rot, scale, dx, dy = _glyph_jitter(seed_key, glyph_index, ch, params)
            glyph_index += 1
            glyph = _make_glyph_tile(font, ch, fill, stroke, outline_width)
            if abs(scale - 1.0) > 1e-3:
                glyph = glyph.resize(
                    (max(1, int(glyph.width * scale)), max(1, int(glyph.height * scale))),
                    resample=Image.BICUBIC,
                )
            if abs(rot) > 1e-3:
                glyph = glyph.rotate(rot, expand=True, resample=Image.BICUBIC)
            if sfx.vertical:
                center_x = margin + band * (line_index + 0.5) + dx * cell
                center_y = margin + advance * (char_index + 0.5) + dy * cell
            else:
                center_x = margin + advance * (char_index + 0.5) + dx * cell
                center_y = margin + band * (line_index + 0.5) + dy * cell
            canvas.alpha_composite(
                glyph, (int(center_x - glyph.width / 2), int(center_y - glyph.height / 2))
            )
    return canvas


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
