"""ページ単位の品質検査（プリフライト）。

重大エラー(error)はCBZ出力を止め、構図上の注意(warning)は出力を許可する。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from . import layout_engine
from .assets import resolve_asset_path
from .prompt_composer import is_non_character_mode
from .renderer import PAGE_SIZE, _panel_box_px, resolve_dialogue_layout
from .schemas import MangaProject, Page, PreflightIssue

# 重なり・縦横比などの許容しきい値。
BUBBLE_OVERLAP_RATIO = 0.25
ASPECT_TOLERANCE = 0.35
GUTTER_MIN_RATIO = 0.4  # 設定ガターのこの割合未満は「狭すぎ」
GUTTER_MAX_ABS = 0.08  # ページ比でこれを超える隣接間は「広すぎ」


def preflight_project(manga: MangaProject, export_dir: Path | None = None) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for index, page in enumerate(manga.pages):
        issues.extend(preflight_page(manga, page, index, export_dir))
    return issues


def preflight_page(
    manga: MangaProject,
    page: Page,
    page_index: int | None = None,
    export_dir: Path | None = None,
) -> list[PreflightIssue]:
    if page_index is None:
        page_index = next((i for i, item in enumerate(manga.pages) if item.page == page.page), 0)
    issues: list[PreflightIssue] = []
    issues.extend(_check_dialogue_fit(manga, page))
    issues.extend(_check_bubble_overlap(manga, page))
    issues.extend(_check_tail_range(page))
    issues.extend(_check_reading_order(manga, page))
    issues.extend(_check_gutters(manga, page))
    issues.extend(_check_layout_repetition(manga, page, page_index))
    issues.extend(_check_image_aspect(page, export_dir))
    issues.extend(_check_subject_mode_characters(page))
    issues.extend(_check_overlay_occlusion(page))
    issues.extend(_check_assets(page, export_dir))
    return issues


def _check_dialogue_fit(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        box = _panel_box_px(panel)
        for dialogue in panel.dialogue:
            _bubble, layout = resolve_dialogue_layout(dialogue, box, manga.typography)
            if not layout.fits:
                # テキストは切り捨てないため出力は止めず、注意（警告）として知らせる。
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="dialogue_overflow",
                        message=f"台詞が大きく、コマに対して窮屈です（{dialogue.speaker}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                    )
                )
    return issues


def _bubble_boxes(manga: MangaProject, page: Page) -> list[tuple[str, tuple[int, int, int, int]]]:
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for panel in page.panels:
        panel_box = _panel_box_px(panel)
        for dialogue in panel.dialogue:
            bubble, _layout = resolve_dialogue_layout(dialogue, panel_box, manga.typography)
            boxes.append((panel.panel_id, bubble))
    return boxes


def _overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    dx = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    dy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return dx * dy


def _check_bubble_overlap(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    boxes = _bubble_boxes(manga, page)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (pid_a, box_a), (pid_b, box_b) = boxes[i], boxes[j]
            overlap = _overlap_area(box_a, box_b)
            if overlap <= 0:
                continue
            area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
            area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
            smaller = max(1, min(area_a, area_b))
            if overlap / smaller >= BUBBLE_OVERLAP_RATIO:
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="bubble_overlap",
                        message=f"吹き出しが重なっています（{pid_a} と {pid_b}）",
                        page=page.page,
                        panel_id=pid_a,
                    )
                )
    return issues


def _check_tail_range(page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        for dialogue in panel.dialogue:
            tail = dialogue.tail
            if tail is None or not tail.enabled:
                continue
            x, y = tail.tip
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="tail_out_of_range",
                        message=f"吹き出しのしっぽ先端がコマ外です（{panel.panel_id}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                    )
                )
    return issues


def _check_reading_order(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    if not page.reading_order:
        return []
    panel_ids = [panel.panel_id for panel in page.panels]
    if len(page.reading_order) != len(panel_ids) or set(page.reading_order) != set(panel_ids):
        return [
            PreflightIssue(
                level="error",
                code="invalid_reading_order",
                message="読み順に重複、欠落、または存在しないコマがあります",
                page=page.page,
            )
        ]
    rtl = manga.reading_direction != "ltr"
    boxes = [panel.bbox for panel in page.panels]
    order_indices = layout_engine.compute_reading_order(boxes, rtl=rtl)
    geometric = [page.panels[i].panel_id for i in order_indices]
    if geometric != list(page.reading_order):
        return [
            PreflightIssue(
                level="warning",
                code="reading_order_reversed",
                message="保存された読み順が右上→左下の自然な順序と一致しません",
                page=page.page,
            )
        ]
    return []


def _check_gutters(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    gutter = manga.page_layout.gutter
    panels = page.panels
    for i in range(len(panels)):
        for j in range(i + 1, len(panels)):
            a = panels[i].bbox
            b = panels[j].bbox
            ax0, ay0, aw, ah = a
            bx0, by0, bw, bh = b
            ax1, ay1 = ax0 + aw, ay0 + ah
            bx1, by1 = bx0 + bw, by0 + bh
            # 重なり（負のガター）。
            if (
                _overlap_area(
                    (int(ax0 * 1000), int(ay0 * 1000), int(ax1 * 1000), int(ay1 * 1000)),
                    (int(bx0 * 1000), int(by0 * 1000), int(bx1 * 1000), int(by1 * 1000)),
                )
                > 0
            ):
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="panels_overlap",
                        message=f"コマが重なっています（{panels[i].panel_id} と {panels[j].panel_id}）",
                        page=page.page,
                        panel_id=panels[i].panel_id,
                    )
                )
                continue
            y_overlap = min(ay1, by1) - max(ay0, by0)
            x_overlap = min(ax1, bx1) - max(ax0, bx0)
            gap = None
            if y_overlap > 0:  # 左右に隣接
                gap = max(bx0 - ax1, ax0 - bx1)
            elif x_overlap > 0:  # 上下に隣接
                gap = max(by0 - ay1, ay0 - by1)
            if gap is None or gap < 0:
                continue
            if 0 < gap < gutter * GUTTER_MIN_RATIO:
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="gutter_too_small",
                        message=f"コマ間隔が狭すぎます（{panels[i].panel_id} と {panels[j].panel_id}）",
                        page=page.page,
                        panel_id=panels[i].panel_id,
                    )
                )
            elif gap > GUTTER_MAX_ABS:
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="gutter_too_large",
                        message=f"コマ間隔が広すぎます（{panels[i].panel_id} と {panels[j].panel_id}）",
                        page=page.page,
                        panel_id=panels[i].panel_id,
                    )
                )
    return issues


def _check_layout_repetition(
    manga: MangaProject, page: Page, page_index: int
) -> list[PreflightIssue]:
    if page_index < 2 or not page.layout_family:
        return []
    prev1 = manga.pages[page_index - 1].layout_family
    prev2 = manga.pages[page_index - 2].layout_family
    if page.layout_family == prev1 == prev2:
        return [
            PreflightIssue(
                level="warning",
                code="layout_repetition",
                message=f"同型レイアウト（{page.layout_family}）が3ページ以上続いています",
                page=page.page,
            )
        ]
    return []


def _check_image_aspect(page: Page, export_dir: Path | None = None) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    page_w, page_h = PAGE_SIZE
    for panel in page.panels:
        if not panel.image_asset:
            continue
        try:
            image_path = (
                resolve_asset_path(panel.image_asset, export_dir)
                if export_dir is not None
                else Path(panel.image_asset)
            )
        except ValueError:
            continue
        if not image_path.exists():
            continue
        try:
            with Image.open(image_path) as image:
                img_w, img_h = image.size
        except Exception:
            continue
        if img_w <= 0 or img_h <= 0:
            continue
        panel_aspect = (panel.bbox[2] * page_w) / max(1.0, panel.bbox[3] * page_h)
        image_aspect = img_w / img_h
        if abs(image_aspect - panel_aspect) / panel_aspect > ASPECT_TOLERANCE:
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="aspect_mismatch",
                    message=f"生成画像の縦横比がコマと大きく異なります（{panel.panel_id}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                )
            )
    return issues


def _check_subject_mode_characters(page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        if is_non_character_mode(panel) and panel.characters:
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="insert_panel_has_characters",
                    message=f"小物/手/背景コマに不要なキャラクター指定があります（{panel.panel_id}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                )
            )
    return issues


def _check_overlay_occlusion(page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    order = page.reading_order or [panel.panel_id for panel in page.panels]
    rank = {panel_id: index for index, panel_id in enumerate(order)}
    panel_boxes = {panel.panel_id: panel.bbox for panel in page.panels}
    for overlay in page.overlay_elements:
        if overlay.layer != "front":
            continue
        source_rank = rank.get(overlay.source_panel_id, len(order))
        ox0, oy0, ow, oh = overlay.box
        obox = (ox0, oy0, ox0 + ow, oy0 + oh)
        for panel_id, bbox in panel_boxes.items():
            if panel_id == overlay.source_panel_id or panel_id in overlay.occluded_by_panel_ids:
                continue
            pbox = (bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3])
            if (
                _overlap_area(
                    tuple(int(v * 1000) for v in obox),  # type: ignore[arg-type]
                    tuple(int(v * 1000) for v in pbox),  # type: ignore[arg-type]
                )
                <= 0
            ):
                continue
            if rank.get(panel_id, len(order)) < source_rank:
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="overlay_hides_earlier_panel",
                        message=f"オーバーフレームが先に読むコマ（{panel_id}）を隠しています",
                        page=page.page,
                        panel_id=panel_id,
                    )
                )
    return issues


def _check_assets(page: Page, export_dir: Path | None) -> list[PreflightIssue]:
    if export_dir is None:
        return []
    issues: list[PreflightIssue] = []
    references: list[tuple[str, str | None]] = []
    for panel in page.panels:
        references.append((panel.panel_id, panel.image_asset))
    for overlay in page.overlay_elements:
        references.append((overlay.source_panel_id, overlay.asset))
        references.append((overlay.source_panel_id, overlay.mask_asset))
    for panel_id, asset in references:
        if not asset:
            continue
        try:
            valid = resolve_asset_path(asset, export_dir).is_file()
        except ValueError:
            valid = False
        if not valid:
            issues.append(
                PreflightIssue(
                    level="error",
                    code="asset_unavailable",
                    message=f"参照アセットを読み込めません: {asset}",
                    page=page.page,
                    panel_id=panel_id or None,
                )
            )
    return issues
