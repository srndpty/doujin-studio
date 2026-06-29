"""preflightと品質検査のテスト。"""

from __future__ import annotations

from pathlib import Path

from backend.app import layout_engine, preflight
from backend.app.schemas import (
    BalloonTail,
    Dialogue,
    MangaProject,
    OverlayElement,
    Page,
    Panel,
    PanelCharacter,
)
from tests.helpers import (
    create_stub_project as create_generated_project,
)
from tests.helpers import (
    make_stub_client as make_client,
)


def _two_panel_page(overlay: OverlayElement | None = None) -> MangaProject:
    return MangaProject(
        title="overlay",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="grid",
                reading_order=["p01_01", "p01_02"],
                overlay_elements=[overlay] if overlay else [],
                panels=[
                    Panel(panel_id="p01_01", bbox=(0.05, 0.05, 0.9, 0.42), shot="t"),
                    Panel(panel_id="p01_02", bbox=(0.05, 0.52, 0.9, 0.42), shot="t"),
                ],
            )
        ],
    )


def test_preflight_flags_dialogue_clipped_as_error() -> None:
    # 商用品質では、最小サイズでも収まらない台詞（文字切れ）は出力前エラーにする（領域3）。
    manga = MangaProject(
        title="overflow",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0.05, 0.05, 0.2, 0.1),
                        shot="t",
                        dialogue=[Dialogue(speaker="a", text="あ" * 300, box=(0.0, 0.0, 0.5, 0.5))],
                    )
                ],
            )
        ],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    clipped = [issue for issue in issues if issue.code == "dialogue_clipped"]
    assert clipped and all(issue.level == "error" for issue in clipped)


def test_preflight_tail_out_of_range_and_insert_characters() -> None:
    manga = MangaProject(
        title="warn",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0.05, 0.05, 0.9, 0.9),
                        shot="t",
                        subject_mode="prop_insert",
                        characters=["hero"],
                        dialogue=[
                            Dialogue(speaker="a", text="やあ", tail=BalloonTail(tip=(1.15, 0.5)))
                        ],
                    )
                ],
            )
        ],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "tail_out_of_range" in codes
    assert "insert_panel_has_characters" in codes
    assert all(
        issue.level == "warning"
        for issue in issues
        if issue.code in {"tail_out_of_range", "insert_panel_has_characters"}
    )


def test_preflight_reading_order_reversal_and_overlay_occlusion() -> None:
    overlay = OverlayElement(
        id="ov", source_panel_id="p01_02", box=(0.05, 0.05, 0.9, 0.42), layer="front"
    )
    manga = _two_panel_page(overlay)
    # 読み順を逆転させる。
    manga.pages[0].reading_order = ["p01_02", "p01_01"]
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "reading_order_reversed" in codes
    # overlay(source=p01_02)が先に読むp01_01を隠している（逆順だとp01_02が先なので隠さない）。
    # 自然順に戻すと隠蔽警告が出る。
    manga.pages[0].reading_order = ["p01_01", "p01_02"]
    issues2 = preflight.preflight_page(manga, manga.pages[0])
    assert any(issue.code == "overlay_hides_earlier_panel" for issue in issues2)


def test_preflight_overlay_scale_extends_occlusion() -> None:
    # 右(p01_01)が先、左(p01_02)が後の読み順。overlayはp01_02発で、box単体では
    # 右コマに重ならないが、scaleで右へはみ出して先に読むp01_01を隠す。
    overlay = OverlayElement(
        id="ov", source_panel_id="p01_02", box=(0.45, 0.4, 0.05, 0.1), scale=3.0, layer="front"
    )
    manga = MangaProject(
        title="ov",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="grid",
                reading_order=["p01_01", "p01_02"],
                overlay_elements=[overlay],
                panels=[
                    Panel(panel_id="p01_01", bbox=(0.5, 0.05, 0.45, 0.9), shot="t"),
                    Panel(panel_id="p01_02", bbox=(0.05, 0.05, 0.45, 0.9), shot="t"),
                ],
            )
        ],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    assert any(issue.code == "overlay_hides_earlier_panel" for issue in issues)
    # 倍率1ならはみ出さず検出されない。
    manga.pages[0].overlay_elements[0].scale = 1.0
    issues2 = preflight.preflight_page(manga, manga.pages[0])
    assert not any(issue.code == "overlay_hides_earlier_panel" for issue in issues2)


def test_preflight_three_column_row_has_no_false_gutter_warning() -> None:
    # montageの3分割（1行3コマ）。左右端は間に中央コマがあるので隣接扱いしない。
    boxes = layout_engine.build_page_layout(3, "montage")
    panels = [Panel(panel_id=f"p01_{k + 1:02d}", bbox=boxes[k], shot="t") for k in range(3)]
    manga = MangaProject(
        title="g",
        pages=[Page(page=1, theme="t", layout_template="grid", panels=panels)],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "gutter_too_large" not in codes
    assert "panels_overlap" not in codes


def test_preflight_layout_repetition() -> None:
    pages = []
    for page_number in range(1, 4):
        pages.append(
            Page(
                page=page_number,
                theme="t",
                layout_template="grid",
                layout_family="dialogue",
                panels=[
                    Panel(panel_id=f"p{page_number:02d}_01", bbox=(0.05, 0.05, 0.9, 0.9), shot="t")
                ],
            )
        )
    manga = MangaProject(title="rep", pages=pages)
    issues = preflight.preflight_page(manga, manga.pages[2], page_index=2)
    assert any(issue.code == "layout_repetition" for issue in issues)


def test_preflight_visual_rhythm_warnings() -> None:
    panels = [
        Panel(
            panel_id=f"p01_{index + 1:02d}",
            bbox=(0.05, 0.05 + index * 0.25, 0.9, 0.2),
            shot="close-up",
            background_density="white",
        )
        for index in range(3)
    ]
    manga = MangaProject(
        title="rhythm",
        pages=[Page(page=1, theme="t", layout_template="grid", panels=panels)],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "shot_repetition" in codes
    assert "background_density_repetition" in codes
    rhythm = [issue for issue in issues if issue.code == "shot_repetition"][0]
    assert rhythm.category == "rhythm"
    assert rhythm.suggestion


def test_preflight_story_structure_and_character_regions() -> None:
    panels = [
        Panel(
            panel_id="p01_01",
            bbox=(0.05, 0.05, 0.9, 0.2),
            shot="wide",
            role="dialogue",
        ),
        Panel(
            panel_id="p01_02",
            bbox=(0.05, 0.3, 0.9, 0.2),
            shot="medium",
            role="dialogue",
            characters=["a", "b"],
            character_layout=[PanelCharacter(id="a", position="upper_left")],
        ),
        Panel(
            panel_id="p01_03",
            bbox=(0.05, 0.55, 0.9, 0.2),
            shot="close-up",
            role="dialogue",
        ),
        Panel(
            panel_id="p01_04",
            bbox=(0.05, 0.8, 0.9, 0.15),
            shot="close-up",
            role="dialogue",
        ),
    ]
    manga = MangaProject(
        title="structure",
        pages=[Page(page=1, theme="t", layout_template="grid", panels=panels)],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert {"page_goal_missing", "emotional_curve_missing", "page_peak_missing"} <= codes
    region = [issue for issue in issues if issue.code == "character_region_missing"][0]
    assert region.category == "character"
    assert "region_box" in region.suggestion


def test_preflight_with_body_checks_posted_manga_without_saving(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        # ページ1をレンダリングしてrender_status=doneにする。
        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        assert (
            client.post(
                f"/api/projects/{project_id}/pages/1/render?revision={revision}"
            ).status_code
            == 200
        )
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        # 本文側にだけ収まらない長文を仕込む（DBは変更しない）。
        dialogue = manga["pages"][0]["panels"][0]["dialogue"][0]
        dialogue["text"] = "あ" * 300
        dialogue["box"] = [0.0, 0.0, 0.12, 0.08]
        manga["pages"][0]["panels"][0]["bbox"] = [0.05, 0.05, 0.18, 0.1]

        response = client.post(f"/api/projects/{project_id}/pages/1/preflight", json=manga)
        assert response.status_code == 200
        body = response.json()
        codes = {issue["code"] for issue in body["errors"]}
        assert "dialogue_clipped" in codes  # 本文の内容が検査される（文字切れはエラー）

        # 非破壊: DBは変わらず、render_statusはdoneのまま、台詞も元のまま。
        after = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        assert after["pages"][0]["render_status"] == "done"
        assert after["pages"][0]["panels"][0]["dialogue"][0]["text"] != "あ" * 300
