"""自動レイアウト選択（背面大ゴマ・裁ち落とし・縦ぶち抜き・重ね小コマ）のテスト。"""

from __future__ import annotations

from backend.app import layout_engine
from backend.app.layout_engine import BLEED, auto_assign_frames, relayout_page
from backend.app.schemas import Page, PageLayoutSettings, Panel


def _page(panels: list[Panel]) -> Page:
    return Page(page=1, theme="t", layout_template="o", panels=panels)


def _panel(panel_id: str, bbox, role: str, emphasis: int, **kwargs) -> Panel:
    return Panel(
        panel_id=panel_id, bbox=bbox, shot="medium", role=role, emphasis=emphasis, **kwargs
    )


def test_hero_becomes_background_and_small_panel_becomes_cut_in() -> None:
    hero = _panel("p1", (0.05, 0.05, 0.9, 0.5), "reveal", 4, characters=["a", "b"])
    insert = _panel("p2", (0.05, 0.6, 0.3, 0.3), "reaction", 2)
    filler = _panel("p3", (0.5, 0.6, 0.45, 0.3), "dialogue", 2)
    page = _page([hero, insert, filler])

    auto_assign_frames(page, PageLayoutSettings())

    assert hero.frame_role == "background"
    assert hero.frame_points is not None
    assert hero.z_index == 0
    # 接した左右上端は裁ち落とし（ページ外へはみ出す）。
    xs = [x for x, _ in hero.frame_points]
    assert min(xs) <= -BLEED + 1e-9
    # 最小の隣接小コマが重ねコマ（手前）になる。
    assert insert.frame_role == "cut_in"
    assert insert.z_index == 2
    assert insert.frame_points is not None
    # bboxは変えない（読み順・ガター検査と整合）。
    assert insert.bbox == (0.05, 0.6, 0.3, 0.3)
    assert filler.frame_role == "normal" and filler.frame_points is None


def test_single_character_tall_hero_becomes_vertical_splash() -> None:
    hero = _panel("p1", (0.7, 0.05, 0.25, 0.9), "reveal", 5, characters=["a"])
    other = _panel("p2", (0.05, 0.05, 0.6, 0.4), "dialogue", 2)
    page = _page([hero, other])

    auto_assign_frames(page, PageLayoutSettings())

    assert hero.frame_role == "vertical_splash"
    assert hero.frame_points is not None
    ys = [y for _, y in hero.frame_points]
    assert min(ys) <= -BLEED + 1e-9 and max(ys) >= 1.0 + BLEED - 1e-9


def test_secondary_impactful_edge_panel_bleeds() -> None:
    # 見せ場role(HERO)が無いページ。強い action コマが端に接していれば裁ち落としにする。
    action = _panel("p1", (0.0, 0.05, 0.5, 0.9), "action", 4)
    talk = _panel("p2", (0.55, 0.05, 0.4, 0.9), "dialogue", 2)
    page = _page([action, talk])

    auto_assign_frames(page, PageLayoutSettings())

    assert action.frame_role == "bleed"
    assert action.frame_points is not None
    assert talk.frame_role == "normal"


def test_diagonal_shape_points_panel_is_left_untouched() -> None:
    diagonal = _panel(
        "p1",
        (0.05, 0.05, 0.9, 0.5),
        "action",
        4,
        shape_points=[(0.12, 0.0), (1.0, 0.0), (0.88, 1.0), (0.0, 1.0)],
    )
    small = _panel("p2", (0.05, 0.6, 0.3, 0.3), "reaction", 2)
    page = _page([diagonal, small])

    auto_assign_frames(page, PageLayoutSettings())

    # 斜めコマ(shape_points)はframe_pointsで上書きしない。
    assert diagonal.frame_points is None
    assert diagonal.shape_points is not None


def test_plain_dialogue_page_gets_no_special_frames() -> None:
    panels = [
        _panel("p1", (0.05, 0.05, 0.42, 0.42), "dialogue", 2),
        _panel("p2", (0.52, 0.05, 0.42, 0.42), "dialogue", 2),
        _panel("p3", (0.05, 0.52, 0.42, 0.42), "dialogue", 2),
    ]
    page = _page(panels)
    auto_assign_frames(page, PageLayoutSettings())
    assert all(panel.frame_role == "normal" and panel.frame_points is None for panel in panels)


def test_auto_assign_is_idempotent() -> None:
    hero = _panel("p1", (0.05, 0.05, 0.9, 0.5), "reveal", 4, characters=["a", "b"])
    insert = _panel("p2", (0.05, 0.6, 0.3, 0.3), "reaction", 2)
    page = _page([hero, insert])
    auto_assign_frames(page, PageLayoutSettings())
    first = (hero.frame_points, hero.frame_role, insert.frame_points, insert.frame_role)
    auto_assign_frames(page, PageLayoutSettings())
    second = (hero.frame_points, hero.frame_role, insert.frame_points, insert.frame_role)
    assert first == second


def test_auto_assign_preserves_manual_frame_and_recomputes_auto_frame() -> None:
    hero = _panel(
        "p1",
        (0.05, 0.05, 0.9, 0.5),
        "reveal",
        4,
        characters=["a", "b"],
        frame_points=[(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)],
        frame_role="bleed",
        frame_source="manual",
        z_index=5,
    )
    insert = _panel("p2", (0.05, 0.6, 0.3, 0.3), "reaction", 2)
    page = _page([hero, insert])
    manual = (hero.frame_points, hero.frame_role, hero.frame_source, hero.z_index)

    auto_assign_frames(page, PageLayoutSettings())
    first_auto = (insert.frame_points, insert.frame_role, insert.frame_source, insert.z_index)
    auto_assign_frames(page, PageLayoutSettings())
    second_auto = (insert.frame_points, insert.frame_role, insert.frame_source, insert.z_index)

    assert (hero.frame_points, hero.frame_role, hero.frame_source, hero.z_index) == manual
    assert first_auto == second_auto
    assert insert.frame_source == "auto"


def test_relayout_page_assigns_frames_for_hero() -> None:
    hero = _panel("p1", (0.0, 0.0, 1.0, 1.0), "reveal", 5, characters=["a", "b"])
    other = _panel("p2", (0.0, 0.0, 1.0, 1.0), "reaction", 2)
    page = _page([hero, other])
    relayout_page(page, PageLayoutSettings(), rtl=True, family="reveal")
    # 再提案後、見せ場コマに特殊枠が付く（family=revealは末尾を大ゴマにする）。
    assert any(panel.frame_role != "normal" for panel in page.panels)
    # 生成された全frame_pointsはモデル検証を通る（再読込で壊れない）。
    for panel in page.panels:
        if panel.frame_points is not None:
            Panel.model_validate(panel.model_dump())


def test_layout_families_unchanged() -> None:
    # 既存のファミリー一覧は維持（auto_assign_framesは下敷きを置き換えない）。
    assert "reveal" in layout_engine.LAYOUT_FAMILIES
