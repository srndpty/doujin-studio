"""preflight自動修正（autofix）のテスト。"""

from __future__ import annotations

from backend.app import preflight
from backend.app.autofix import autofix_manga
from backend.app.schemas import (
    MAX_CROP_SCALE,
    BalloonTail,
    Character,
    Dialogue,
    MangaProject,
    Page,
    Panel,
    PanelCharacter,
    PreflightIssue,
    Sfx,
)


def _project(panel: Panel) -> MangaProject:
    return MangaProject(
        title="t",
        characters=[Character(id="a", display_name="A", trigger_prompt="char_a")],
        pages=[Page(page=1, theme="th", layout_template="single", panels=[panel])],
    )


def test_autofix_strips_blank_risk_prompt() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        characters=["a"],
        prompt="white, empty space, smile",
    )
    project = _project(panel)
    messages = autofix_manga(project)
    assert messages
    tags = project.pages[0].panels[0].prompt.split(", ")
    assert "white" not in tags and "empty space" not in tags
    # 修正後は再検査で白紙リスク警告が消える。
    remaining = [
        issue
        for issue in preflight.preflight_page(project, project.pages[0], 0)
        if issue.code == "prompt_blank_risk"
    ]
    assert not remaining


def test_autofix_strips_blank_risk_generation_prompt() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        characters=["a"],
    )
    panel.generation.prompt = "1girl, white background"
    project = _project(panel)
    autofix_manga(project)
    prompt = project.pages[0].panels[0].generation.prompt
    assert "white background" not in prompt
    assert "visible subject" in prompt


def test_autofix_converts_monologue_cloud_to_caption() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        characters=["a"],
        dialogue=[
            Dialogue(speaker="a", text="…", kind="monologue", balloon="cloud", balloon_auto=False)
        ],
    )
    project = _project(panel)
    autofix_manga(project)
    assert project.pages[0].panels[0].dialogue[0].balloon == "caption"


def test_autofix_converts_known_english_sfx() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        sfx=[Sfx(text="bang")],
    )
    project = _project(panel)
    autofix_manga(project)
    assert project.pages[0].panels[0].sfx[0].text == "バン"


def test_autofix_redirects_tail_to_speaker() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        characters=["a"],
        character_layout=[PanelCharacter(id="a", position="lower_left")],
        dialogue=[
            Dialogue(
                speaker="a",
                text="やあ",
                on_screen=True,
                tail=BalloonTail(enabled=True, tip=(0.95, 0.05)),
            )
        ],
    )
    project = _project(panel)
    autofix_manga(project)
    tip = project.pages[0].panels[0].dialogue[0].tail.tip
    # lower_left(0.27,0.73)付近へ寄る。
    assert tip[0] < 0.5 and tip[1] > 0.5


def test_autofix_page_scope_only_targets_requested_page() -> None:
    panel1 = Panel(panel_id="p1", bbox=(0.0, 0.0, 1.0, 1.0), shot="m", prompt="white, smile")
    panel2 = Panel(panel_id="p2", bbox=(0.0, 0.0, 1.0, 1.0), shot="m", prompt="blank, smile")
    project = MangaProject(
        title="t",
        pages=[
            Page(page=1, theme="th", layout_template="single", panels=[panel1]),
            Page(page=2, theme="th", layout_template="single", panels=[panel2]),
        ],
    )
    autofix_manga(project, page_number=2)
    assert "white" in project.pages[0].panels[0].prompt  # 1ページは未修正
    assert "blank" not in project.pages[1].panels[0].prompt  # 2ページは修正


def test_autofix_enlarges_crop_for_subject_too_small_issue() -> None:
    panel = Panel(panel_id="p1", bbox=(0.0, 0.0, 1.0, 1.0), shot="m")
    project = _project(panel)
    issue = PreflightIssue(
        level="warning",
        code="subject_too_small",
        message="小さい",
        page=1,
        panel_id="p1",
    )
    autofix_manga(project, issues=[issue])
    generation = project.pages[0].panels[0].generation
    assert generation.crop_scale > 1.0


def test_autofix_keeps_manual_crop_offset_when_enlarging() -> None:
    """端に寄せた被写体のoffsetを維持したままcropだけ拡大する（領域5）。"""
    panel = Panel(panel_id="p1", bbox=(0.0, 0.0, 1.0, 1.0), shot="m")
    panel.generation.crop_offset_x = 0.4
    panel.generation.crop_offset_y = -0.3
    project = _project(panel)
    issue = PreflightIssue(
        level="warning", code="subject_too_small", message="小さい", page=1, panel_id="p1"
    )
    autofix_manga(project, issues=[issue])
    generation = project.pages[0].panels[0].generation
    assert generation.crop_scale > 1.0
    # offsetは0へ戻さず維持する（被写体を画面外へ追い出さない）。
    assert generation.crop_offset_x == 0.4
    assert generation.crop_offset_y == -0.3


def test_autofix_noop_for_subject_too_small_at_crop_limit() -> None:
    """crop上限のコマはautofix対象でも変更されない（領域6と整合）。"""
    panel = Panel(panel_id="p1", bbox=(0.0, 0.0, 1.0, 1.0), shot="m")
    panel.generation.crop_scale = MAX_CROP_SCALE
    project = _project(panel)
    issue = PreflightIssue(
        level="warning", code="subject_too_small", message="小さい", page=1, panel_id="p1"
    )
    changes = autofix_manga(project, issues=[issue])
    assert not any(change.code == "subject_too_small" for change in changes)
    assert project.pages[0].panels[0].generation.crop_scale == MAX_CROP_SCALE
