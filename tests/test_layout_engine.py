"""レイアウトエンジンと読み順・再提案のテスト。"""

from __future__ import annotations

from pathlib import Path

from conftest import (
    create_stub_project as create_generated_project,
)
from conftest import (
    make_stub_client as make_client,
)

from backend.app import layout_engine, story
from backend.app.schemas import (
    MangaProject,
    ScriptStage,
)


def test_build_layout_produces_valid_boxes_for_all_families() -> None:
    for family in layout_engine.LAYOUT_FAMILIES:
        for count in range(1, 8):
            boxes = layout_engine.build_page_layout(count, family)
            assert len(boxes) == count
            for left, top, width, height in boxes:
                assert width > 0 and height > 0
                assert left >= -0.0001 and top >= -0.0001
                assert left + width <= 1.0001 and top + height <= 1.0001


def test_reading_order_is_right_to_left_top_to_bottom() -> None:
    boxes = layout_engine.build_page_layout(4, "dialogue", rtl=True)
    # 2x2グリッド。先頭が右上になるよう右詰めで生成される。
    assert boxes[0][0] > boxes[1][0]  # 1番目が2番目より右
    order = layout_engine.compute_reading_order(boxes, rtl=True)
    assert order == [0, 1, 2, 3]
    # 左綴じ(ltr)では先頭が左上。
    ltr = layout_engine.build_page_layout(4, "dialogue", rtl=False)
    assert ltr[0][0] < ltr[1][0]


def test_choose_family_avoids_adjacent_repeat() -> None:
    family = layout_engine.choose_family("", 1, 4, 4, previous_family="dialogue")
    assert family != "dialogue"
    # ヒントがあれば尊重する。
    assert layout_engine.choose_family("punchline", 1, 4, 4, None) == "punchline"


def test_reveal_and_punchline_allocate_a_large_panel() -> None:
    reveal = layout_engine.build_page_layout(3, "reveal")
    # 末尾コマ（見せ場）が他より高い。
    assert reveal[-1][3] > reveal[0][3]
    punch = layout_engine.build_page_layout(3, "punchline")
    assert punch[-1][3] > reveal[-1][3] - 0.0001


def test_no_consecutive_equal_full_width_rows() -> None:
    # action系で全幅が連続しても高さが変わる（リズム規則）。
    rows, weights = layout_engine._rows_and_weights(4, "action")
    for i in range(1, len(rows)):
        if rows[i] == 1 and rows[i - 1] == 1:
            assert abs(weights[i] - weights[i - 1]) > 1e-6


def test_script_to_manga_sets_composition_metadata() -> None:
    base = MangaProject(title="本")
    script = ScriptStage.model_validate(
        {
            "pages": [
                {
                    "page": 1,
                    "panels": [
                        {"shot": "wide", "dialogue": [{"speaker": "a", "text": "やあ"}]}
                        for _ in range(3)
                    ],
                },
                {"page": 2, "panels": [{"shot": "close", "dialogue": []} for _ in range(2)]},
            ]
        }
    )
    manga = story.script_to_manga(script, base)
    page1 = manga.pages[0]
    assert page1.layout_family in layout_engine.LAYOUT_FAMILIES
    assert page1.reading_order == [panel.panel_id for panel in page1.panels]
    assert page1.panels[0].role  # 役割が付く
    assert 1 <= page1.panels[0].emphasis <= 5
    # 隣接ページは同じファミリーを避ける。
    assert manga.pages[0].layout_family != manga.pages[1].layout_family or len(
        manga.pages[1].panels
    ) != len(manga.pages[0].panels)


def test_script_to_manga_preserves_directing_metadata() -> None:
    base = MangaProject(title="本")
    script = ScriptStage.model_validate(
        {
            "pages": [
                {
                    "page": 1,
                    "panels": [
                        {
                            "shot": "close-up",
                            "role": "emotional peak",
                            "emotion": "動揺",
                            "background_density": "white",
                            "composition_notes": "eyes dominate the panel",
                            "text_safe_area": [0.55, 0.05, 0.35, 0.3],
                            "dialogue": [{"speaker": "a", "text": "……"}],
                        }
                    ],
                    "page_goal": "主人公の動揺を見せる",
                    "emotional_curve": ["平静", "動揺"],
                }
            ]
        }
    )
    manga = story.script_to_manga(script, base)
    panel = manga.pages[0].panels[0]
    assert panel.role == "emotional_peak"
    assert panel.emotion == "動揺"
    assert panel.background_density == "white"
    assert panel.composition_notes == "eyes dominate the panel"
    assert panel.text_safe_area == (0.55, 0.05, 0.35, 0.3)
    assert panel.emphasis == 5
    assert manga.pages[0].page_goal == "主人公の動揺を見せる"
    assert manga.pages[0].emotional_curve == ["平静", "動揺"]


def test_layout_suggest_api_repropose_keeps_content(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        before = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        page1 = before["pages"][0]
        original_dialogue = page1["panels"][0]["dialogue"]
        original_ids = [p["panel_id"] for p in page1["panels"]]
        original_bbox = page1["panels"][0]["bbox"]

        response = client.post(
            f"/api/projects/{project_id}/pages/1/layout/suggest?revision="
            f"{client.get(f'/api/projects/{project_id}').json()['revision']}",
            json={"family": "montage"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["result"]["layout_family"] == "montage"
        new_page = payload["project"]["manga_json"]["pages"][0]
        # コマ・台詞は維持され、座標と読み順だけ更新される。
        assert [p["panel_id"] for p in new_page["panels"]] == original_ids
        assert new_page["panels"][0]["dialogue"] == original_dialogue
        assert new_page["reading_order"] == original_ids
        assert new_page["layout_locked"] is False
        assert new_page["panels"][0]["bbox"] != original_bbox


def test_layout_locked_survives_manga_json_save(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        manga["pages"][0]["layout_locked"] = True
        saved = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        ).json()
        assert saved["project"]["manga_json"]["pages"][0]["layout_locked"] is True
