"""検証済みレイアウトエンジン。

構図（コマ数・役割・強調度・テンプレートファミリー）から、検証済みの
コマ座標(bbox)へ決定的に変換する。LLMには座標を作らせない。
リズム規則（全幅コマの連続回避、隣接ページの同型反復回避、見せ場への面積配分）
と、右上から左下への読み順をここで一元的に扱う。
"""

from __future__ import annotations

import math

from .generator import compute_generation_size
from .schemas import Page, PageLayoutSettings

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
        # 全幅コマが連続しないよう、単独行は重みを交互に変える。
        weights = _vary_full_width(rows)
        return rows, weights

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
        rows: list[int] = []
        remaining = panel_count
        toggle = True
        while remaining > 0:
            if toggle or remaining == 1:
                rows.append(1)
                remaining -= 1
            else:
                rows.append(2)
                remaining -= 2
            toggle = not toggle
        weights = [1.3 if cols == 1 else 1.0 for cols in rows]
        return rows, _vary_full_width(rows, weights)

    # 既定は会話グリッド。
    rows = _distribute_rows(panel_count, 2)
    return rows, _vary_full_width(rows)


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
    for cols, weight in zip(rows, weights):
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


def derive_role(panel_index: int, panel_count: int, page_index: int, total_pages: int, has_dialogue: bool) -> str:
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
    for panel, box in zip(page.panels, boxes):
        panel.bbox = box
        width, height = compute_generation_size(box)
        panel.generation.width = width
        panel.generation.height = height
    page.layout_family = chosen
    page.reading_order = [panel.panel_id for panel in page.panels]
    page.layout_locked = False
    page.render_status = "pending"
    page.rendered_at = None
    return page


def derive_emphasis(role: str) -> int:
    return {
        "establish": 4,
        "reveal": 4,
        "punchline": 5,
        "action": 3,
        "dialogue": 2,
        "silent": 2,
        "montage": 1,
    }.get(role, 2)
