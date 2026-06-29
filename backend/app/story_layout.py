from __future__ import annotations

import math

from .generator import LAYOUTS

PANEL_LAYOUTS: dict[int, dict[str, list[tuple[float, float, float, float]]]] = {
    1: {"default": [(0.06, 0.05, 0.88, 0.9)]},
    2: {
        "default": [(0.06, 0.05, 0.88, 0.43), (0.06, 0.52, 0.88, 0.43)],
        "horizontal": [(0.06, 0.05, 0.42, 0.9), (0.52, 0.05, 0.42, 0.9)],
    },
    3: {
        "default": LAYOUTS["reaction_3"],
        "punchline": LAYOUTS["punchline_3"],
    },
    4: {
        "default": LAYOUTS["conversation_4"],
        "vertical": LAYOUTS["vertical_3_start"],
    },
}


def distribute_rows(panel_count: int, max_cols: int = 3) -> list[int]:
    """コマ数を1行あたり最大max_colsで均等な行構成へ分配する。"""
    panel_count = max(1, panel_count)
    n_rows = math.ceil(panel_count / max_cols)
    base, extra = divmod(panel_count, n_rows)
    # 余りは上の行から1コマずつ足す（読み始めを情報量多めにする）。
    return [base + (1 if i < extra else 0) for i in range(n_rows)]


def grid_layout(
    panel_count: int, margin: float = 0.04, gap: float = 0.012
) -> list[tuple[float, float, float, float]]:
    """任意コマ数を行グリッドのbboxへ自動配置する（1〜4以外のフォールバック）。"""
    rows = distribute_rows(panel_count)
    usable_h = 1 - 2 * margin - gap * (len(rows) - 1)
    row_h = usable_h / len(rows)
    boxes: list[tuple[float, float, float, float]] = []
    y = margin
    for cols in rows:
        usable_w = 1 - 2 * margin - gap * (cols - 1)
        col_w = usable_w / cols
        x = margin
        for _ in range(cols):
            boxes.append((round(x, 4), round(y, 4), round(col_w, 4), round(row_h, 4)))
            x += col_w + gap
        y += row_h + gap
    return boxes


def select_layout(panel_count: int, hint: str) -> list[tuple[float, float, float, float]]:
    options = PANEL_LAYOUTS.get(panel_count)
    if options:
        if hint and hint in options:
            return options[hint]
        return options["default"]
    # 5コマ以上は動的グリッドで配置する。
    return grid_layout(panel_count)
