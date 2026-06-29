"""写植とテキストレイアウトのテスト。"""

from __future__ import annotations

from backend.app import fonts, typeset
from backend.app.renderer import resolve_dialogue_layout
from backend.app.schemas import (
    Dialogue,
    MangaProject,
    Page,
    Panel,
)


def test_dialogue_font_falls_back_to_biz_ud_when_genei_absent() -> None:
    # 源暎アンチックが無ければ退避フォント（源暎以外）を選ぶ。
    # 日本語フォントが一切無い環境（CIなど）ではNoneでも可（描画はPIL既定へ退避）。
    path = fonts.find_dialogue_font_path()
    if path is not None and not fonts.dialogue_font_is_primary():
        assert "GenEiAntique".casefold() not in path.name.casefold()
    listed = {item["id"]: item for item in fonts.list_fonts()}
    assert listed["genei_antique"]["is_primary"] is True


def test_dialogue_font_size_is_not_capped_by_project_default() -> None:
    manga = MangaProject(
        title="font-size",
        typography={"default_font_size": 34, "min_font_size": 20},
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0.0, 0.0, 1.0, 1.0),
                        shot="wide",
                        dialogue=[Dialogue(speaker="a", text="大", font_size=60)],
                    )
                ],
            )
        ],
    )
    _bubble, layout = resolve_dialogue_layout(
        manga.pages[0].panels[0].dialogue[0], (0, 0, 1200, 1700), manga.typography
    )
    assert layout.font_size == 60


def test_max_lines_marks_layout_as_not_fitting() -> None:
    layout = typeset.layout_text(
        "一二三四五六七八九十",
        None,
        80,
        500,
        vertical=False,
        default_size=30,
        min_size=30,
        max_lines=1,
    )
    assert layout.fits is False


def test_vertical_typeset_keeps_all_characters() -> None:
    text = "これはABC123とテスト、長い台詞。"
    layout = typeset.layout_text(text, None, 200, 600, vertical=True, default_size=34, min_size=26)
    flattened = "".join(token[1] for line in layout.columns for token in line)
    # 縦中横や回転で順序は保つ。全文字が保持される（切り捨てない）。
    for ch in text:
        assert ch in flattened


def test_long_text_warns_when_it_cannot_fit() -> None:
    text = "あ" * 400
    layout = typeset.layout_text(text, None, 60, 60, vertical=True, default_size=34, min_size=26)
    assert layout.fits is False
    assert any("収まりません" in warning for warning in layout.warnings)
