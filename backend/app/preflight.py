"""ページ単位の品質検査（プリフライト）。

重大エラー(error)はCBZ出力を止め、構図上の注意(warning)は出力を許可する。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

from . import layout_engine
from .assets import resolve_asset_path
from .preflight_geometry import bbox_overlaps, overlap_area, panel_between, unit_box_iou
from .prompt_composer import is_non_character_mode
from .prompt_normalizer import blank_risk_tags
from .renderer import (
    PAGE_SIZE,
    TEXT_FLOOR_SIZE,
    _panel_box_px,
    resolve_dialogue_layout,
    sfx_bbox_px,
)
from .schemas import Character, Dialogue, MangaProject, Page, Panel, PreflightIssue
from .story import normalize_sfx_text, panel_shape_allowed

# 画像メトリクス検査のしきい値。
WHITE_PIXEL_MIN = 240  # 各チャンネルがこの値以上なら「ほぼ白」とみなす
EMPTY_NONWHITE_RATIO = 0.02  # 非白ピクセル比率がこれ未満なら空コマ（白紙）
SUBJECT_BBOX_MIN_RATIO = 0.18  # 非白領域bboxがコマ面積のこれ未満なら被写体が小さすぎる
MONOCHROME_SATURATION_MAX = 0.08  # 平均彩度がこれ未満なら白黒・低彩度
TAIL_SPEAKER_MAX_DISTANCE = 0.45  # しっぽ先端と話者領域中心の許容距離（コマ正規化）
# 画像メトリクスのしきい値を緩めるrole（演出意図のある白背景・余韻コマ）。
RELAXED_IMAGE_ROLES = {"silent", "transition", "aftermath"}

# 重なり・縦横比などの許容しきい値。
BUBBLE_OVERLAP_RATIO = 0.25
ASPECT_TOLERANCE = 0.35
GUTTER_MIN_RATIO = 0.4  # 設定ガターのこの割合未満は「狭すぎ」
GUTTER_MAX_ABS = 0.08  # ページ比でこれを超える隣接間は「広すぎ」
SFX_OVERLAP_RATIO = 0.3  # 擬音同士がこの割合以上重なれば衝突とみなす
SFX_BUBBLE_OVERLAP_RATIO = 0.25  # 擬音が吹き出しとこの割合以上重なれば衝突
SFX_OUT_OF_BOUNDS_RATIO = 0.18  # 擬音がコマ外へこの割合以上はみ出せば警告


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
    issues.extend(_check_story_structure(page))
    issues.extend(_check_visual_rhythm(page))
    issues.extend(_check_image_aspect(page, export_dir))
    issues.extend(_check_subject_mode_characters(page))
    issues.extend(_check_sfx_collisions(manga, page))
    issues.extend(_check_sfx_text_language(page))
    issues.extend(_check_monologue_balloon(page))
    issues.extend(_check_silent_panels(page))
    issues.extend(_check_prompt_blank_risk(page))
    issues.extend(_check_panel_shapes(page))
    issues.extend(_check_tail_speaker(page))
    issues.extend(_check_character_setup(manga, page))
    issues.extend(_check_character_regions(page))
    issues.extend(_check_overlay_occlusion(page))
    issues.extend(_check_image_metrics(manga, page, export_dir))
    issues.extend(_check_assets(page, export_dir))
    return issues


def _check_dialogue_fit(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        box = _panel_box_px(panel)
        for dialogue in panel.dialogue:
            # 縮小判定は台詞ごとの要求サイズ基準にする（font_sizeを明示的に小さくした
            # 台詞を「縮小された」と誤判定しない）。
            requested_size = max(
                dialogue.font_size or manga.typography.default_font_size, TEXT_FLOOR_SIZE
            )
            _bubble, layout = resolve_dialogue_layout(dialogue, box, manga.typography)
            if not layout.fits:
                # 最小サイズでも全文が収まらない＝文字切れ。商用品質では出力前エラーにする（領域3）。
                issues.append(
                    PreflightIssue(
                        level="error",
                        code="dialogue_clipped",
                        message=f"台詞が最小サイズでも吹き出しに収まりません（{dialogue.speaker}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                    )
                )
            elif layout.font_size < requested_size:
                # 収まってはいるがフォント縮小が必要なほど窮屈。警告として知らせる。
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


def _check_bubble_overlap(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    boxes = _bubble_boxes(manga, page)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (pid_a, box_a), (pid_b, box_b) = boxes[i], boxes[j]
            overlap = overlap_area(box_a, box_b)
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


_GUTTER_EPS = 1e-6


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
            if bbox_overlaps(a, b):
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
            gap: float | None = None
            blocked = False
            if y_overlap > _GUTTER_EPS and x_overlap <= _GUTTER_EPS:  # 左右に隣接
                band = (max(ay0, by0), min(ay1, by1))
                gap_span = (ax1, bx0) if bx0 >= ax1 else (bx1, ax0)
                gap = gap_span[1] - gap_span[0]
                blocked = panel_between(panels, i, j, gap_span, band, horizontal=True)
            elif x_overlap > _GUTTER_EPS and y_overlap <= _GUTTER_EPS:  # 上下に隣接
                band = (max(ax0, bx0), min(ax1, bx1))
                gap_span = (ay1, by0) if by0 >= ay1 else (by1, ay0)
                gap = gap_span[1] - gap_span[0]
                blocked = panel_between(panels, i, j, gap_span, band, horizontal=False)
            # 隣接していない（間にコマがある／斜め）ペアはガター判定から除外する。
            if gap is None or gap < 0 or blocked:
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


def _check_story_structure(page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    if len(page.panels) >= 3 and not page.page_goal.strip():
        issues.append(
            PreflightIssue(
                level="warning",
                code="page_goal_missing",
                message="ページ目的が未設定です",
                page=page.page,
                category="rhythm",
                suggestion="このページで読者の感情をどこへ進めるかをpage_goalへ短く設定してください",
                fixable=False,
            )
        )
    if len(page.panels) >= 3 and len(page.emotional_curve) < 2:
        issues.append(
            PreflightIssue(
                level="warning",
                code="emotional_curve_missing",
                message="感情曲線が不足しています",
                page=page.page,
                category="rhythm",
                suggestion="導入、反応、転換、余韻などページ内の感情ビートを2件以上設定してください",
                fixable=False,
            )
        )
    roles = {_normalize_rhythm_token(panel.role) for panel in page.panels if panel.role}
    if len(page.panels) >= 4 and not (roles & {"reveal", "punchline", "emotional_peak"}):
        issues.append(
            PreflightIssue(
                level="warning",
                code="page_peak_missing",
                message="ページ内に見せ場コマがありません",
                page=page.page,
                category="rhythm",
                suggestion="1コマはreveal/punchline/emotional_peakのいずれかにして面積や演出を強めてください",
                fixable=False,
            )
        )
    return issues


def _normalize_rhythm_token(value: str) -> str:
    return (value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def _check_visual_rhythm(page: Page) -> list[PreflightIssue]:
    """同じ画角・白背景の連続を検出し、ネーム段階の単調さを警告する。"""
    issues: list[PreflightIssue] = []
    panels = page.panels
    if len(panels) < 3:
        return issues

    for index in range(len(panels) - 2):
        window = panels[index : index + 3]
        shots = [_normalize_rhythm_token(panel.shot or panel.camera) for panel in window]
        if shots[0] and len(set(shots)) == 1:
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="shot_repetition",
                    message="同じ画角のコマが3つ続いています",
                    page=page.page,
                    panel_id=window[0].panel_id,
                    category="rhythm",
                    suggestion="close-up、medium shot、wide shotなど画角を交互に変えてください",
                    fixable=False,
                )
            )
            break

    pale_background = {"none", "white", "blank"}
    for index in range(len(panels) - 2):
        window = panels[index : index + 3]
        densities = [
            _normalize_rhythm_token(panel.background_density or "")
            for panel in window
            if not is_non_character_mode(panel)
        ]
        if len(densities) == 3 and all(value in pale_background for value in densities):
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="background_density_repetition",
                    message="白背景または背景なしの人物コマが3つ続いています",
                    page=page.page,
                    panel_id=window[0].panel_id,
                    category="rhythm",
                    suggestion="少なくとも1コマはlight/full背景にするか、白背景の演出理由を明確にしてください",
                    fixable=False,
                )
            )
            break
    return issues


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


def _check_sfx_collisions(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    """擬音のコマ外はみ出し・吹き出し衝突・擬音同士の重なりを検出する（領域4）。

    擬音はコマ外へのはみ出しを許すため、擬音同士の重なりはページ全体で総当たり判定し、
    隣接コマの擬音がガター上で重なるケースも拾う。
    """
    issues: list[PreflightIssue] = []
    bubble_boxes = _bubble_boxes(manga, page)
    # ページ全体の擬音矩形を集める（コマ外はみ出し・吹き出し衝突は個別に判定）。
    page_sfx: list[tuple[str, str, tuple[int, int, int, int]]] = []
    for panel in page.panels:
        panel_box = _panel_box_px(panel)
        for sfx in panel.sfx:
            box = sfx_bbox_px(sfx, panel_box)
            sfx_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
            # コマ外はみ出し（擬音面積に対する超過分）。
            inside = overlap_area(box, panel_box)
            if (sfx_area - inside) / sfx_area >= SFX_OUT_OF_BOUNDS_RATIO:
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="sfx_out_of_bounds",
                        message=f"擬音「{sfx.text}」がコマ外へはみ出しています（{panel.panel_id}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                    )
                )
            # 吹き出しとの衝突。
            for _pid, bubble in bubble_boxes:
                overlap = overlap_area(box, bubble)
                if overlap > 0 and overlap / sfx_area >= SFX_BUBBLE_OVERLAP_RATIO:
                    issues.append(
                        PreflightIssue(
                            level="warning",
                            code="sfx_bubble_collision",
                            message=f"擬音「{sfx.text}」が吹き出しと重なっています（{panel.panel_id}）",
                            page=page.page,
                            panel_id=panel.panel_id,
                        )
                    )
                    break
            page_sfx.append((panel.panel_id, sfx.text, box))
    # 擬音同士の重なり（同一中心への配置・隣接コマのガター上の重なりを含む）。
    for i in range(len(page_sfx)):
        pid_i, text_i, box_i = page_sfx[i]
        for j in range(i + 1, len(page_sfx)):
            pid_j, text_j, box_j = page_sfx[j]
            overlap = overlap_area(box_i, box_j)
            if overlap <= 0:
                continue
            area_i = (box_i[2] - box_i[0]) * (box_i[3] - box_i[1])
            area_j = (box_j[2] - box_j[0]) * (box_j[3] - box_j[1])
            smaller = max(1, min(area_i, area_j))
            if overlap / smaller >= SFX_OVERLAP_RATIO:
                same = pid_i == pid_j
                where = f"{pid_i}" if same else f"{pid_i} と {pid_j}"
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="sfx_overlap",
                        message=f"擬音「{text_i}」と「{text_j}」が重なっています（{where}）",
                        page=page.page,
                        panel_id=pid_i,
                    )
                )
    return issues


def _check_character_setup(manga: MangaProject, page: Page) -> list[PreflightIssue]:
    """人物コマで、同一性を保てる設定（trigger/LoRA/参照画像）が不足していないか（領域1/5）。"""
    issues: list[PreflightIssue] = []
    by_id: dict[str, Character] = {character.id: character for character in manga.characters}
    # 同じキャラの警告がコマ数ぶん重複しないよう、ページ内で1回に集約する。
    warned: set[str] = set()
    for panel in page.panels:
        if is_non_character_mode(panel):
            continue
        for character_id in panel.characters:
            if character_id in warned:
                continue
            character = by_id.get(character_id)
            if character is None:
                warned.add(character_id)
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="character_unknown",
                        message=f"未登録のキャラクターを参照しています（{character_id}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                    )
                )
                continue
            weak_trigger = (
                not character.trigger_prompt.strip()
                or character.trigger_prompt.strip() == character.display_name.strip()
            )
            has_lora = bool(character.lora_name and character.lora_node_id)
            has_reference = bool(
                character.reference_image_asset and character.reference_load_node_id
            )
            if weak_trigger and not has_lora and not has_reference:
                warned.add(character_id)
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="character_setup_incomplete",
                        message=(
                            f"「{character.display_name}」に有効なtrigger/LoRA/参照画像が無く、"
                            "同一性を保てません"
                        ),
                        page=page.page,
                        panel_id=panel.panel_id,
                    )
                )
    return issues


def _check_character_regions(page: Page) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        if is_non_character_mode(panel) or len(panel.characters) < 2:
            continue
        by_id = {entry.id: entry for entry in panel.character_layout}
        missing = [
            character_id
            for character_id in panel.characters
            if character_id not in by_id or by_id[character_id].region_box is None
        ]
        if missing:
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="character_region_missing",
                    message=f"複数キャラコマに人物領域がありません（{', '.join(missing)}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                    category="character",
                    suggestion="各キャラのregion_boxを設定し、regional workflowで領域分離できるようにしてください",
                    fixable=False,
                )
            )
        entries = [
            entry
            for entry in panel.character_layout
            if entry.id in panel.characters and entry.region_box is not None
        ]
        for left_index, left_entry in enumerate(entries):
            assert left_entry.region_box is not None
            for right_entry in entries[left_index + 1 :]:
                assert right_entry.region_box is not None
                iou = unit_box_iou(left_entry.region_box, right_entry.region_box)
                if iou >= 0.5:
                    issues.append(
                        PreflightIssue(
                            level="warning",
                            code="character_region_overlap",
                            message=(
                                "複数キャラの人物領域が大きく重なっています"
                                f"（{left_entry.id}, {right_entry.id}）"
                            ),
                            page=page.page,
                            panel_id=panel.panel_id,
                            category="character",
                            suggestion=(
                                "各キャラのregion_boxを分けるか、positionを左右・上下に分散してください"
                            ),
                            fixable=False,
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
        # 実際の描画範囲はbox * scale（renderer.draw_overlayと一致させる）。
        ox0, oy0, ow, oh = overlay.box
        obox = (ox0, oy0, ox0 + ow * overlay.scale, oy0 + oh * overlay.scale)
        for panel_id, bbox in panel_boxes.items():
            if panel_id == overlay.source_panel_id or panel_id in overlay.occluded_by_panel_ids:
                continue
            pbox = (bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3])
            if (
                overlap_area(
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


def _is_ascii_letter_dominant(text: str) -> bool:
    """英字主体の文字列か（日本語化されていない英字SFXの検出用）。"""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    ascii_letters = [ch for ch in letters if ch.isascii()]
    return len(ascii_letters) / len(letters) >= 0.6


def _check_sfx_text_language(page: Page) -> list[PreflightIssue]:
    """英字主体の擬音を検出する。"""
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        for sfx in panel.sfx:
            if _is_ascii_letter_dominant(sfx.text):
                # 辞書で日本語化できる語だけ自動修正可能にする。未知語は手動修正を促す。
                convertible = normalize_sfx_text(sfx.text) != sfx.text
                issues.append(
                    PreflightIssue(
                        level="error",
                        code="sfx_english_text",
                        message=f"英字の擬音「{sfx.text}」が日本語へ変換されていません",
                        page=page.page,
                        panel_id=panel.panel_id,
                        category="sfx",
                        suggestion="擬音は日本語表記にしてください（例: bang→バン）",
                        fixable=convertible,
                    )
                )
    return issues


def _check_monologue_balloon(page: Page) -> list[PreflightIssue]:
    """独白の丸泡(cloud)乱用を警告し、矩形キャプションへの変更を促す。"""
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        for dialogue in panel.dialogue:
            if dialogue.kind == "monologue" and dialogue.balloon == "cloud":
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="monologue_cloud_balloon",
                        message=f"独白に丸い吹き出し(cloud)を使っています（{dialogue.speaker}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                        category="balloon",
                        suggestion="独白は矩形のキャプション(caption)を基本にしてください",
                        fixable=True,
                    )
                )
    return issues


def _check_prompt_blank_risk(page: Page) -> list[PreflightIssue]:
    """白紙コマを誘発するprompt（white/blank/empty space等の単独指定）を検出する。

    生成promptは生成時に正規化されるが、保存済みのpanel.prompt/composition_notesに
    残る誘発タグを自動修正できるよう、ここでfixableな警告として知らせる（領域: 品質ゲート）。
    """
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        hits: list[str] = []
        for text in (panel.prompt, panel.composition_notes):
            hits.extend(blank_risk_tags(text))
        if hits:
            unique = list(dict.fromkeys(hits))
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="prompt_blank_risk",
                    message=f"白紙化を招くprompt語があります（{', '.join(unique)}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                    category="image_quality",
                    suggestion=(
                        "white/blank/empty space等の単独指定を除去し、背景指定はsimple_background"
                        "等のbooruタグへ寄せてください"
                    ),
                    fixable=True,
                )
            )
    return issues


# 無言が演出意図として自然な役割（沈黙・間・余韻）。これらは無言でも警告しない。
INTENTIONAL_SILENT_ROLES = {"silent", "transition", "aftermath", "montage"}


def _check_silent_panels(page: Page) -> list[PreflightIssue]:
    """無言の人物コマが多すぎるページを検出する（「無言のコマが多すぎる」対策）。

    人物が描かれるコマ（小物・手・背景コマを除く）で台詞が無く、かつ無言が自然な役割
    （silent/transition/aftermath/montage）でもないものを「無言コマ」とみなす。これが
    人物コマの過半かつ3枚以上なら、台詞・反応の追加を促す。台詞の中身は編集判断のため
    自動修正はしない（fixable=False）。
    """
    character_panels = [
        panel for panel in page.panels if not is_non_character_mode(panel) and panel.characters
    ]
    if len(character_panels) < 3:
        return []
    silent = [
        panel
        for panel in character_panels
        if not panel.dialogue
        and _normalize_rhythm_token(panel.role) not in INTENTIONAL_SILENT_ROLES
    ]
    if len(silent) >= 3 and len(silent) >= 0.6 * len(character_panels):
        return [
            PreflightIssue(
                level="warning",
                code="too_many_silent_panels",
                message=f"無言の人物コマが多すぎます（{len(silent)}/{len(character_panels)}枚）",
                page=page.page,
                category="rhythm",
                suggestion=(
                    "台詞や短い反応・モノローグを加えるか、無言の意図があるコマは"
                    "role=silent/transition/aftermathにしてください"
                ),
                fixable=False,
            )
        ]
    return []


def _check_panel_shapes(page: Page) -> list[PreflightIssue]:
    """演出意図のない変形コマ(shape_points)を警告する。"""
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        if panel.shape_points and not panel_shape_allowed(panel.role, panel.composition_notes):
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="unmotivated_panel_shape",
                    message=f"演出意図のない変形コマです（{panel.panel_id}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                    category="layout",
                    suggestion=(
                        "変形はaction/reveal/emotional_peak/punchlineで動き・衝撃・見せ場の意図が"
                        "あるコマだけにし、会話・準備・心情・余韻は矩形にしてください"
                    ),
                    fixable=False,
                )
            )
    return issues


_POSITION_FRACTION: dict[str, tuple[float, float]] = {
    "upper_left": (0.27, 0.27),
    "upper_right": (0.73, 0.27),
    "lower_left": (0.27, 0.73),
    "lower_right": (0.73, 0.73),
    "center": (0.5, 0.5),
}


def _speaker_center(dialogue: Dialogue, panel: Panel) -> tuple[float, float] | None:
    """話者の人物領域中心（コマ正規化0..1）。解決できなければNone。"""
    entry = next((item for item in panel.character_layout if item.id == dialogue.speaker), None)
    if entry is None:
        return None
    if entry.region_box is not None:
        rx, ry, rw, rh = entry.region_box
        return (rx + rw / 2, ry + rh / 2)
    return _POSITION_FRACTION.get(entry.position, (0.5, 0.5))


def _check_tail_speaker(page: Page) -> list[PreflightIssue]:
    """明示したしっぽ先端が話者領域から遠いコマを警告する。"""
    issues: list[PreflightIssue] = []
    for panel in page.panels:
        for dialogue in panel.dialogue:
            if not dialogue.on_screen or dialogue.balloon in {"caption", "none"}:
                continue
            tail = dialogue.tail
            if tail is None or not tail.enabled:
                continue
            center = _speaker_center(dialogue, panel)
            if center is None:
                continue
            distance = math.hypot(tail.tip[0] - center[0], tail.tip[1] - center[1])
            if distance > TAIL_SPEAKER_MAX_DISTANCE:
                issues.append(
                    PreflightIssue(
                        level="warning",
                        code="tail_not_pointing_to_speaker",
                        message=f"吹き出しのしっぽが話者（{dialogue.speaker}）の方を向いていません",
                        page=page.page,
                        panel_id=panel.panel_id,
                        category="balloon",
                        suggestion="しっぽ先端を話者の人物領域へ向けてください",
                        fixable=True,
                    )
                )
    return issues


def _panel_image_metrics(image_path: Path) -> dict[str, float] | None:
    """コマ画像の白比率・被写体bbox比率・平均彩度を粗く測る。"""
    try:
        with Image.open(image_path) as image:
            small = image.convert("RGB").resize((64, 64))
    except Exception:
        return None
    pixels = list(small.getdata())
    total = len(pixels) or 1
    width = small.width
    white_count = 0
    nonwhite_count = 0
    saturation_sum = 0.0
    min_x = min_y = width
    max_x = max_y = -1
    for index, (r, g, b) in enumerate(pixels):
        high = max(r, g, b)
        low = min(r, g, b)
        saturation_sum += 0.0 if high == 0 else (high - low) / high
        if low >= WHITE_PIXEL_MIN:
            white_count += 1
            continue
        nonwhite_count += 1
        x, y = index % width, index // width
        min_x, min_y = min(min_x, x), min(min_y, y)
        max_x, max_y = max(max_x, x), max(max_y, y)
    if max_x < 0:
        bbox_ratio = 0.0
    else:
        bbox_ratio = ((max_x - min_x + 1) * (max_y - min_y + 1)) / total
    return {
        "white_ratio": white_count / total,
        "nonwhite_ratio": nonwhite_count / total,
        "subject_bbox_ratio": bbox_ratio,
        "mean_saturation": saturation_sum / total,
    }


def _check_image_metrics(
    manga: MangaProject, page: Page, export_dir: Path | None
) -> list[PreflightIssue]:
    """生成画像の白紙・小被写体・低彩度を画像統計で検出する。"""
    issues: list[PreflightIssue] = []
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
        metrics = _panel_image_metrics(image_path)
        if metrics is None:
            continue
        density = (panel.background_density or "").strip().casefold()
        role = _normalize_rhythm_token(panel.role)
        relaxed = density in {"white", "none"} and role in RELAXED_IMAGE_ROLES
        if metrics["nonwhite_ratio"] < EMPTY_NONWHITE_RATIO:
            if not relaxed:
                issues.append(
                    PreflightIssue(
                        level="error",
                        code="empty_panel_image",
                        message=f"コマ画像がほぼ白紙です（{panel.panel_id}）",
                        page=page.page,
                        panel_id=panel.panel_id,
                        category="image_quality",
                        suggestion="被写体が描かれた画像を生成・採用してください",
                        fixable=False,
                    )
                )
            continue
        if (
            not relaxed
            and not is_non_character_mode(panel)
            and metrics["subject_bbox_ratio"] < SUBJECT_BBOX_MIN_RATIO
        ):
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="subject_too_small",
                    message=f"被写体が小さすぎ、余白が多すぎます（{panel.panel_id}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                    category="image_quality",
                    suggestion="被写体を大きく配置する構図・crop・promptへ調整してください",
                    fixable=False,
                )
            )
        if (
            manga.color_policy == "full_color"
            and metrics["mean_saturation"] < MONOCHROME_SATURATION_MAX
        ):
            issues.append(
                PreflightIssue(
                    level="warning",
                    code="monochrome_panel",
                    message=f"フルカラー方針に対して低彩度または白黒のコマです（{panel.panel_id}）",
                    page=page.page,
                    panel_id=panel.panel_id,
                    category="style",
                    suggestion="演出意図がない場合はフルカラーで再生成してください",
                    fixable=False,
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
