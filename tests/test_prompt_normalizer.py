"""prompt正規化（白紙誘発語の除去・booruタグ寄せ）のテスト。"""

from __future__ import annotations

from backend.app import preflight
from backend.app.prompt_composer import compose_panel_prompts
from backend.app.prompt_normalizer import blank_risk_tags, normalize_prompt
from backend.app.schemas import Character, MangaProject, Page, Panel


def _project_with_panel(panel: Panel) -> MangaProject:
    return MangaProject(
        title="t",
        characters=[Character(id="a", display_name="A", trigger_prompt="char_a")],
        pages=[Page(page=1, theme="th", layout_template="single", panels=[panel])],
    )


def test_normalize_removes_blank_inducing_tags() -> None:
    result = normalize_prompt("1girl, white, blank, empty space, smile")
    assert "white" not in result.prompt.split(", ")
    assert "blank" not in result.prompt.split(", ")
    assert "empty space" not in result.prompt.split(", ")
    assert "1girl" in result.prompt and "smile" in result.prompt
    assert set(result.removed) == {"white", "blank", "empty space"}
    assert result.changed


def test_normalize_keeps_compound_white_tags() -> None:
    # 単独の white は白紙を招くが、white hair / white shirt は被写体の一部なので残す。
    result = normalize_prompt("1girl, white hair, white shirt")
    assert result.prompt == "1girl, white hair, white shirt"
    assert not result.changed


def test_normalize_steers_white_background_to_booru() -> None:
    result = normalize_prompt("1girl, white background")
    assert "simple background" in result.prompt
    assert "visible subject" in result.prompt
    assert "clear foreground" in result.prompt
    assert "white background" not in result.prompt
    assert result.replaced == [
        ("white background", "simple background, visible subject, clear foreground")
    ]


def test_blank_risk_tags_lists_offenders() -> None:
    assert blank_risk_tags("1girl, white, white background") == ["white", "white background"]
    assert blank_risk_tags("1girl, smile") == []


def test_compose_panel_prompts_strips_blank_risk() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        characters=["a"],
        prompt="white, empty space, looking at viewer",
    )
    positive, _negative = compose_panel_prompts(_project_with_panel(panel), panel)
    tags = positive.split(", ")
    assert "white" not in tags
    assert "empty space" not in tags
    assert "looking at viewer" in tags


def test_preflight_flags_blank_risk_prompt_as_fixable() -> None:
    panel = Panel(
        panel_id="p1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        characters=["a"],
        prompt="white, smile",
    )
    project = _project_with_panel(panel)
    issues = preflight.preflight_page(project, project.pages[0], 0)
    blank = [issue for issue in issues if issue.code == "prompt_blank_risk"]
    assert len(blank) == 1
    assert blank[0].fixable
