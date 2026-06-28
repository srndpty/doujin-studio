"""検証済みレイアウトエンジン。

構図（コマ数・役割・強調度・テンプレートファミリー）から、検証済みの
コマ座標(bbox)へ決定的に変換する。LLMには座標を作らせない。
リズム規則（全幅コマの連続回避、隣接ページの同型反復回避、見せ場への面積配分）
と、右上から左下への読み順をここで一元的に扱う。
"""

from __future__ import annotations

import math

from .generator import compute_generation_size
from .schemas import Page, PageLayoutSettings, Panel

# 裁ち落としでページ外へはみ出す量（frame_pointsの許容範囲-0.05..1.05に合わせる）。
BLEED = 0.05
# コマ枠の頂点が同士で接する/触れると判定する許容差。
EDGE_EPS = 0.02
# 見せ場（背面大ゴマ・裁ち落とし候補）になりうる役割。
HERO_ROLES = frozenset({"reveal", "punchline", "emotional_peak", "establish"})
# 裁ち落とし（端まで塗る）にしてよい役割。
BLEED_ROLES = frozenset({"reveal", "punchline", "emotional_peak", "establish", "action"})
# 重ね小コマ（カットイン）にしてよい小コマの役割。
CUT_IN_ROLES = frozenset({"reaction", "silent", "dialogue", "aftermath", "transition"})

# 拡張テンプレートファミリー。
LAYOUT_FAMILIES = [
    "establish",
    "dialogue",
    "reveal",
    "action",
    "punchline",
    "silent",
    "montage",
]

Box = tuple[float, float, float, float]


def _distribute_rows(panel_count: int, max_cols: int) -> list[int]:
    """コマ数を1行あたり最大max_colsで均等に分配する。"""
    panel_count = max(1, panel_count)
    n_rows = math.ceil(panel_count / max_cols)
    base, extra = divmod(panel_count, n_rows)
    return [base + (1 if i < extra else 0) for i in range(n_rows)]


def _rows_and_weights(panel_count: int, family: str) -> tuple[list[int], list[float]]:
    """ファミリーごとに行のコラム数と高さ重みを決める。"""
    if panel_count <= 1:
        return [1], [1.0]

    if family == "montage":
        rows = _distribute_rows(panel_count, 3)
        return rows, [1.0] * len(rows)

    if family in {"dialogue", "silent"}:
        rows = _distribute_rows(panel_count, 2)
        # 行境界が中央(腹切り)に来ないよう非対称な重みにする。
        return rows, _vary_full_width(rows, _asymmetric_weights(rows))

    if family == "establish":
        # 先頭に状況提示の大ゴマ、残りを分配する。
        rest = _distribute_rows(panel_count - 1, 2)
        rows = [1] + rest
        weights = [1.7] + [1.0] * len(rest)
        return rows, _vary_full_width(rows, weights)

    if family in {"reveal", "punchline"}:
        # 末尾に見せ場の大ゴマ。punchlineはより大きく。
        big = 2.4 if family == "punchline" else 2.0
        head = _distribute_rows(panel_count - 1, 2)
        rows = head + [1]
        weights = [1.0] * len(head) + [big]
        return rows, _vary_full_width(rows, weights)

    if family == "action":
        # 全幅と分割を交互に並べてテンポを作る。
        action_rows: list[int] = []
        remaining = panel_count
        toggle = True
        while remaining > 0:
            if toggle or remaining == 1:
                action_rows.append(1)
                remaining -= 1
            else:
                action_rows.append(2)
                remaining -= 2
            toggle = not toggle
        weights = [1.3 if cols == 1 else 1.0 for cols in action_rows]
        return action_rows, _vary_full_width(action_rows, weights)

    # 既定は会話グリッド。
    rows = _distribute_rows(panel_count, 2)
    return rows, _vary_full_width(rows, _asymmetric_weights(rows))


def _asymmetric_weights(rows: list[int]) -> list[float]:
    """行の高さを交互にずらし、全幅の横線が中央(腹切り)に揃うのを避ける。"""
    return [1.0 + (0.16 if i % 2 == 0 else -0.12) for i in range(len(rows))]


def _vary_full_width(rows: list[int], weights: list[float] | None = None) -> list[float]:
    """同じ高さの全幅コマが連続しないよう重みを微調整する。"""
    weights = list(weights) if weights is not None else [1.0] * len(rows)
    for i in range(1, len(rows)):
        if rows[i] == 1 and rows[i - 1] == 1 and abs(weights[i] - weights[i - 1]) < 1e-6:
            # 連続する全幅コマは高さを変えてリズムを出す。
            weights[i] = weights[i] * 1.25
    return weights


def build_page_layout(
    panel_count: int,
    family: str,
    settings: PageLayoutSettings | None = None,
    rtl: bool = True,
) -> list[Box]:
    """ファミリーと余白設定からコマbboxを読み順（先頭=最初に読む）で返す。"""
    settings = settings or PageLayoutSettings()
    margin = settings.outer_margin
    gutter = settings.gutter
    rows, weights = _rows_and_weights(panel_count, family)

    total_weight = sum(weights) or 1.0
    usable_h = 1.0 - 2 * margin - gutter * (len(rows) - 1)
    usable_h = max(0.05, usable_h)

    boxes: list[Box] = []
    y = margin
    for cols, weight in zip(rows, weights, strict=True):
        row_h = usable_h * (weight / total_weight)
        usable_w = 1.0 - 2 * margin - gutter * (cols - 1)
        col_w = usable_w / cols
        # 右綴じは各行を右から左へ詰める（読み順=先頭が右上）。
        positions = range(cols - 1, -1, -1) if rtl else range(cols)
        for col_index in positions:
            x = margin + col_index * (col_w + gutter)
            boxes.append((round(x, 4), round(y, 4), round(col_w, 4), round(row_h, 4)))
        y += row_h + gutter
    return boxes[:panel_count]


def compute_reading_order(boxes: list[Box], rtl: bool = True) -> list[int]:
    """bbox群を右上→左下（rtl）の読み順インデックスへ並べる。"""
    bands = _row_bands(boxes)
    order: list[int] = []
    for band in bands:
        # 同じ行は右から左（rtl）。
        band.sort(key=lambda idx: boxes[idx][0] + boxes[idx][2] / 2, reverse=rtl)
        order.extend(band)
    return order


def _row_bands(boxes: list[Box]) -> list[list[int]]:
    """縦位置が近いコマを同じ行バンドへまとめる。"""
    indexed = sorted(range(len(boxes)), key=lambda idx: boxes[idx][1])
    bands: list[list[int]] = []
    for idx in indexed:
        top = boxes[idx][1]
        height = boxes[idx][3]
        placed = False
        for band in bands:
            band_top = boxes[band[0]][1]
            band_h = boxes[band[0]][3]
            # 縦の重なりが大きければ同じ行とみなす。
            overlap = min(top + height, band_top + band_h) - max(top, band_top)
            if overlap > 0.4 * min(height, band_h):
                band.append(idx)
                placed = True
                break
        if not placed:
            bands.append([idx])
    bands.sort(key=lambda band: min(boxes[idx][1] for idx in band))
    return bands


def choose_family(
    hint: str,
    page_index: int,
    total_pages: int,
    panel_count: int,
    previous_family: str | None = None,
) -> str:
    """ヒント・ページ位置から適切なファミリーを選ぶ（隣接ページの反復を避ける）。"""
    hint = (hint or "").strip().lower()
    direct = {
        "establish": "establish",
        "establishing": "establish",
        "dialogue": "dialogue",
        "conversation": "dialogue",
        "reveal": "reveal",
        "action": "action",
        "punchline": "punchline",
        "silent": "silent",
        "montage": "montage",
        "vertical": "establish",
        "horizontal": "dialogue",
    }
    family = next((value for key, value in direct.items() if key in hint), "")
    if not family:
        if panel_count == 1:
            family = "reveal"
        elif page_index == 0:
            family = "establish"
        elif page_index == total_pages - 1:
            family = "punchline"
        elif panel_count >= 6:
            family = "montage"
        else:
            family = "dialogue"
    # 隣接ページの同型反復を避ける。
    if previous_family and family == previous_family:
        family = _alternate_family(family, panel_count)
    return family


def _alternate_family(family: str, panel_count: int) -> str:
    alternatives = {
        "establish": "dialogue",
        "dialogue": "action",
        "reveal": "establish",
        "action": "dialogue",
        "punchline": "reveal",
        "silent": "dialogue",
        "montage": "action",
    }
    return alternatives.get(family, "dialogue")


def derive_role(
    panel_index: int, panel_count: int, page_index: int, total_pages: int, has_dialogue: bool
) -> str:
    """コマの役割を位置と台詞有無から推定する（構図メタデータ）。"""
    if panel_count == 1:
        return "reveal"
    if panel_index == 0 and page_index == 0:
        return "establish"
    if panel_index == panel_count - 1:
        return "punchline" if page_index == total_pages - 1 else "reveal"
    if not has_dialogue:
        return "silent"
    return "dialogue"


def relayout_page(
    page: Page,
    settings: PageLayoutSettings | None = None,
    rtl: bool = True,
    family: str | None = None,
    previous_family: str | None = None,
    page_index: int = 0,
    total_pages: int = 1,
) -> Page:
    """ページのコマ座標を再提案する。画像・台詞・SFXは保持する。

    既存のコマ順（=読み順）を維持したままbboxと生成サイズを更新し、
    手動編集ロックは解除（新提案）した状態で返す。
    """
    panel_count = len(page.panels)
    if family in LAYOUT_FAMILIES:
        chosen = family
    else:
        chosen = choose_family(
            page.layout_family or "", page_index, total_pages, panel_count, previous_family
        )
    boxes = build_page_layout(panel_count, chosen, settings, rtl=rtl)
    for panel, box in zip(page.panels, boxes, strict=True):
        panel.bbox = box
        width, height = compute_generation_size(box)
        panel.generation.width = width
        panel.generation.height = height
    page.layout_family = chosen
    page.reading_order = [panel.panel_id for panel in page.panels]
    # 長方形タイルを下敷きに、見せ場へ背面大ゴマ・裁ち落とし・縦ぶち抜き・重ね小コマを自動付与する。
    auto_assign_frames(page, settings, rtl=rtl)
    page.layout_locked = False
    page.render_status = "pending"
    page.rendered_at = None
    return page


def _normalize_role(value: str) -> str:
    return (value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def _clamp_frame(value: float) -> float:
    """frame_pointsの許容範囲(-0.05..1.05)へ丸める。"""
    return max(-BLEED, min(1.0 + BLEED, value))


def _rect_frame(bbox: Box) -> list[tuple[float, float]]:
    x, y, w, h = bbox
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _touched_edges(bbox: Box, margin: float) -> set[str]:
    """コマがページ外周余白に接している辺（裁ち落とし候補）を返す。"""
    x, y, w, h = bbox
    edges: set[str] = set()
    if x <= margin + EDGE_EPS:
        edges.add("left")
    if x + w >= 1.0 - margin - EDGE_EPS:
        edges.add("right")
    if y <= margin + EDGE_EPS:
        edges.add("top")
    if y + h >= 1.0 - margin - EDGE_EPS:
        edges.add("bottom")
    return edges


def _rect_polygon(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [
        (_clamp_frame(x0), _clamp_frame(y0)),
        (_clamp_frame(x1), _clamp_frame(y0)),
        (_clamp_frame(x1), _clamp_frame(y1)),
        (_clamp_frame(x0), _clamp_frame(y1)),
    ]


def _bleed_polygon(
    bbox: Box, edges: set[str], expand_untouched: float = 0.0
) -> list[tuple[float, float]]:
    """接した辺をページ外(裁ち落とし)へ、それ以外を任意量だけ外側へ広げた矩形枠。"""
    x, y, w, h = bbox
    x0, y0, x1, y1 = x, y, x + w, y + h
    x0 = -BLEED if "left" in edges else x0 - expand_untouched
    x1 = 1.0 + BLEED if "right" in edges else x1 + expand_untouched
    y0 = -BLEED if "top" in edges else y0 - expand_untouched
    y1 = 1.0 + BLEED if "bottom" in edges else y1 + expand_untouched
    return _rect_polygon(x0, y0, x1, y1)


def _bbox_area(bbox: Box) -> float:
    return bbox[2] * bbox[3]


def _bbox_center(bbox: Box) -> tuple[float, float]:
    return (bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2)


def _select_hero(panels: list[Panel]) -> Panel | None:
    """ページの見せ場コマ（背面大ゴマ候補）を1枚選ぶ。

    変形コマ(shape_points)は既存の斜めコマ演出を尊重して対象外。役割が見せ場で
    強調度4以上のうち、強調度→面積→出現順で最も強い1枚を選ぶ。
    """
    candidates = [
        panel
        for panel in panels
        if panel.frame_source != "manual"
        and panel.shape_points is None
        and _normalize_role(panel.role) in HERO_ROLES
        and panel.emphasis >= 4
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda panel: (panel.emphasis, _bbox_area(panel.bbox)))


def _cut_in_polygon(small: Box, hero: Box) -> list[tuple[float, float]]:
    """小コマを見せ場コマ側へはみ出させた重ねコマ枠を作る。"""
    over = 0.06
    x, y, w, h = small
    x0, y0, x1, y1 = x, y, x + w, y + h
    sx, sy = _bbox_center(small)
    hx, hy = _bbox_center(hero)
    # 中心差が大きい軸方向へ、見せ場コマへ食い込ませる。
    if abs(hx - sx) >= abs(hy - sy):
        if hx < sx:
            x0 -= over
        else:
            x1 += over
    else:
        if hy < sy:
            y0 -= over
        else:
            y1 += over
    return _rect_polygon(x0, y0, x1, y1)


def auto_assign_frames(
    page: Page, settings: PageLayoutSettings | None = None, rtl: bool = True
) -> None:
    """長方形タイル(bbox)を下敷きに、見せ場コマへ特殊なコマ枠を自動付与する。

    役割・強調度・人物数・ページ目的から、背面大ゴマ(background)、裁ち落とし(bleed)、
    縦ぶち抜き(vertical_splash)、重ね小コマ(cut_in)を選び、frame_points/z_index/
    frame_roleを設定する。bboxは外接矩形のまま変えないため、読み順・ガター等の既存検査と
    整合する（重なりはframe_pointsの拡張とz_indexで表現する）。変形コマ(shape_points)は
    既存の斜めコマ演出として尊重し、本処理の対象外にする。
    """
    settings = settings or PageLayoutSettings()
    margin = settings.outer_margin
    gutter = settings.gutter
    panels = page.panels

    # 既存の自動枠をリセットしてから再付与する（再提案で多重適用しない）。
    # 利用者が調整したmanual枠は保持する。
    # shape_points由来の斜めコマには触れない。
    for panel in panels:
        if panel.frame_source == "manual":
            continue
        if panel.shape_points is None:
            panel.frame_points = None
        panel.frame_role = "normal"
        panel.z_index = 0

    if len(panels) < 2:
        return

    hero = _select_hero(panels)
    assigned: set[str] = set()

    if hero is not None:
        edges = _touched_edges(hero.bbox, margin)
        x, y, w, h = hero.bbox
        single_character = len(hero.characters) == 1
        tall = h >= w and h >= 0.45
        if single_character and tall:
            # 立ち絵の縦ぶち抜き。上下を裁ち落として全段ぶち抜きに見せる。
            hero.frame_points = _bleed_polygon(hero.bbox, {"top", "bottom"})
            hero.frame_role = "vertical_splash"
        else:
            # 背面大ゴマ。接した辺は裁ち落とし、それ以外もガター分だけ広げ、奥に敷く。
            hero.frame_points = _bleed_polygon(hero.bbox, edges, expand_untouched=gutter)
            hero.frame_role = "background"
        hero.frame_source = "auto"
        hero.z_index = 0
        assigned.add(hero.panel_id)

        # 見せ場の手前に重ねる小コマ（カットイン）を1枚選ぶ。
        cut_in = _select_cut_in(panels, hero, assigned)
        if cut_in is not None:
            cut_in.frame_points = _cut_in_polygon(cut_in.bbox, hero.bbox)
            cut_in.frame_role = "cut_in"
            cut_in.frame_source = "auto"
            cut_in.z_index = 2
            assigned.add(cut_in.panel_id)

    # 残りの見せ場コマのうち、ページ端に接するものを裁ち落としにする。
    for panel in panels:
        if (
            panel.panel_id in assigned
            or panel.frame_source == "manual"
            or panel.shape_points is not None
        ):
            continue
        if _normalize_role(panel.role) not in BLEED_ROLES or panel.emphasis < 4:
            continue
        edges = _touched_edges(panel.bbox, margin)
        if not edges:
            continue
        panel.frame_points = _bleed_polygon(panel.bbox, edges)
        panel.frame_role = "bleed"
        panel.frame_source = "auto"
        assigned.add(panel.panel_id)


def _select_cut_in(panels: list[Panel], hero: Panel, assigned: set[str]) -> Panel | None:
    """見せ場コマに隣接する小さな反応・間コマを重ね小コマ候補として選ぶ。"""
    candidates = [
        panel
        for panel in panels
        if panel.panel_id not in assigned
        and panel.frame_source != "manual"
        and panel.shape_points is None
        and panel.emphasis <= 2
        and (
            _normalize_role(panel.role) in CUT_IN_ROLES
            or panel.subject_mode in {"prop_insert", "hand_insert", "reaction"}
        )
        and _is_adjacent(panel.bbox, hero.bbox)
    ]
    if not candidates:
        return None
    # 最も小さいコマを重ねる（見せ場を隠しすぎない）。
    return min(candidates, key=lambda panel: _bbox_area(panel.bbox))


def _is_adjacent(a: Box, b: Box, tolerance: float = 0.06) -> bool:
    """2つのbboxが隣接（辺を共有またはガターを挟んで近接）しているか。"""
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1, bx1, by1 = ax0 + aw, ay0 + ah, bx0 + bw, by0 + bh
    x_gap = max(ax0, bx0) - min(ax1, bx1)
    y_gap = max(ay0, by0) - min(ay1, by1)
    x_overlap = min(ax1, bx1) - max(ax0, bx0)
    y_overlap = min(ay1, by1) - max(ay0, by0)
    # 一方の軸で重なり、もう一方の軸の隙間がガター程度なら隣接とみなす。
    if x_overlap > 0 and y_gap <= tolerance:
        return True
    if y_overlap > 0 and x_gap <= tolerance:
        return True
    return False


def derive_emphasis(role: str) -> int:
    return {
        "establish": 4,
        "reveal": 4,
        "emotional_peak": 5,
        "punchline": 5,
        "action": 3,
        "reaction": 3,
        "dialogue": 2,
        "silent": 2,
        "transition": 2,
        "aftermath": 2,
        "montage": 1,
    }.get(role, 2)
