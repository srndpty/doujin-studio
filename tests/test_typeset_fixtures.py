"""写植レイアウトの共通フィクスチャ検証（領域7）。

tests/fixtures/typeset_cases.json はフロントエンドのプレビュー（typeset-layout.ts）と
最終レンダラー（typeset.layout_text）が同じ収まり判定を返すことを保証する共通フィクスチャ。
本テストはバックエンド側がフィクスチャ通りの font_size / line_count / fits を出すことを確認し、
同じJSONをフロントの typeset-layout.test.ts も突き合わせることで、片側だけの仕様変更を検出する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import typeset
from backend.app.renderer import BUBBLE_INNER_PAD, SHAPE_INSCRIBE

FIXTURE = Path(__file__).parent / "fixtures" / "typeset_cases.json"


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_typeset_fixture_matches_renderer(case: dict) -> None:
    fx, fy = SHAPE_INSCRIBE.get(case["balloon"], (1.05, 1.05))
    inner_w = max(8.0, (case["bubble_w"] - BUBBLE_INNER_PAD * 2) / fx)
    inner_h = max(8.0, (case["bubble_h"] - BUBBLE_INNER_PAD * 2) / fy)
    layout = typeset.layout_text(
        case["text"],
        None,
        inner_w,
        inner_h,
        case["vertical"],
        case["default_size"],
        case["min_size"],
        case["max_lines"],
    )
    expect = case["expect"]
    assert layout.font_size == expect["font_size"]
    assert len(layout.columns) == expect["line_count"]
    assert layout.fits == expect["fits"]
    # 行/列のtoken分割（禁則・手動改行・縦中横の結果）まで一致を要求する（領域5）。
    actual_lines = [[{"kind": kind, "text": text} for kind, text in col] for col in layout.columns]
    assert actual_lines == expect["lines"]
