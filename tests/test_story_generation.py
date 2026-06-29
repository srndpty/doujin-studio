"""段階的story生成と漫画変換のテスト。"""

from __future__ import annotations

from pathlib import Path

from conftest import (
    create_stub_project as create_project,
)
from conftest import (
    make_stub_client as make_client,
)
from conftest import (
    mutation_url,
)
from fastapi.testclient import TestClient

from backend.app import story
from backend.app.schemas import (
    Character,
    MangaProject,
    ScriptStage,
)


def run_full_stub_flow(client: TestClient, project_id: str, target_pages: int) -> str:
    session = client.post(
        mutation_url(client, project_id, "story-sessions"),
        json={"target_pages": target_pages, "instruction": "短い日常話"},
    ).json()
    session_id = session["result"]["id"]
    for stage in ["brief", "plot", "pages", "script"]:
        generated = client.post(f"/api/story-sessions/{session_id}/stages/{stage}/generate")
        assert generated.status_code == 200, generated.text
        assert generated.json()["stages"][stage]["status"] == "draft"
        approved = client.post(f"/api/story-sessions/{session_id}/stages/{stage}/approve")
        assert approved.status_code == 200
        assert approved.json()["stages"][stage]["status"] == "approved"
    return session_id


def test_stub_flow_for_4_8_16_pages(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for target in (4, 8, 16):
            project_id = create_project(client, work_name=f"作品{target}", target_pages=target)
            session_id = run_full_stub_flow(client, project_id, target)
            session = client.get(f"/api/story-sessions/{session_id}").json()
            assert len(session["stages"]["pages"]["data"]["pages"]) == target
            assert len(session["stages"]["script"]["data"]["pages"]) == target


def test_cannot_skip_stage_and_edit_invalidates_downstream(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        session = client.post(
            mutation_url(client, project_id, "story-sessions"), json={"target_pages": 4}
        ).json()
        session_id = session["result"]["id"]

        # brief未承認でplotは生成できない。
        blocked = client.post(f"/api/story-sessions/{session_id}/stages/plot/generate")
        assert blocked.status_code == 400

        for stage in ["brief", "plot", "pages", "script"]:
            client.post(f"/api/story-sessions/{session_id}/stages/{stage}/generate")
            client.post(f"/api/story-sessions/{session_id}/stages/{stage}/approve")

        # 上流briefを編集すると下流データは破棄され、古い出力をready扱いしない。
        current = client.get(f"/api/story-sessions/{session_id}").json()
        brief_data = current["stages"]["brief"]["data"]
        brief_data["tone"] = "しっとり"
        edited = client.put(
            f"/api/story-sessions/{session_id}/stages/brief", json={"data": brief_data}
        ).json()
        assert edited["stages"]["brief"]["status"] == "draft"
        assert edited["stages"]["plot"]["status"] == "empty"
        assert edited["stages"]["plot"]["data"] is None
        assert edited["stages"]["script"]["status"] == "empty"
        assert edited["stages"]["script"]["data"] is None


def test_invalidated_downstream_stage_cannot_be_applied(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        session_id = run_full_stub_flow(client, project_id, 4)

        # plot再生成でpages/scriptは古いdataごと無効化される。
        regenerated = client.post(f"/api/story-sessions/{session_id}/stages/plot/generate")
        assert regenerated.status_code == 200
        assert regenerated.json()["stages"]["pages"]["status"] == "empty"
        assert regenerated.json()["stages"]["script"]["data"] is None

        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        applied = client.post(f"/api/story-sessions/{session_id}/apply?revision={revision}")
        assert applied.status_code == 400
        assert "台本段階" in applied.json()["detail"]


def test_story_session_creation_without_project_change_does_not_consume_revision(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        before = client.get(f"/api/projects/{project_id}").json()["revision"]
        created = client.post(
            f"/api/projects/{project_id}/story-sessions?revision={before}",
            json={"target_pages": 4},
        )
        assert created.status_code == 200
        assert created.json()["project"]["revision"] == before
        assert client.get(f"/api/projects/{project_id}").json()["revision"] == before


def test_apply_and_revision_roundtrip(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        before = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        first_theme = before["pages"][0]["theme"]

        session_id = run_full_stub_flow(client, project_id, 4)
        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        applied = client.post(f"/api/story-sessions/{session_id}/apply?revision={revision}")
        assert applied.status_code == 200
        manga = applied.json()["project"]["manga_json"]
        assert len(manga["pages"]) == 4
        # キャラ・共通promptが維持されること。
        assert manga["characters"][0]["display_name"] == "春香"
        # panel IDとbboxが正しいこと。
        for page in manga["pages"]:
            assert 1 <= len(page["panels"]) <= 4
            for index, panel in enumerate(page["panels"], start=1):
                assert panel["panel_id"] == f"p{page['page']:02d}_{index:02d}"
                left, top, width, height = panel["bbox"]
                assert 0 <= left and 0 <= top and left + width <= 1.0001 and top + height <= 1.0001
                assert panel["image_candidates"] == []
                assert panel["selected_candidate_id"] is None

        revisions = client.get(f"/api/projects/{project_id}/revisions").json()
        assert len(revisions) == 1

        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        restored = client.post(
            f"/api/projects/{project_id}/revisions/{revisions[0]['id']}/restore?revision={revision}"
        ).json()
        assert restored["project"]["manga_json"]["pages"][0]["theme"] == first_theme
        # 復元時も現在状態を新リビジョンとして保存する。
        assert len(client.get(f"/api/projects/{project_id}/revisions").json()) == 2


def test_script_needs_repaneling_detects_all_single_panel() -> None:
    def one() -> dict:
        return {"shot": "wide", "dialogue": []}

    all_single = {"pages": [{"page": 1, "panels": [one()]}, {"page": 2, "panels": [one()]}]}
    assert story.script_needs_repaneling(all_single) is True
    # 1ページでも複数コマがあれば退化ではない。
    mixed = {"pages": [{"page": 1, "panels": [one(), one()]}, {"page": 2, "panels": [one()]}]}
    assert story.script_needs_repaneling(mixed) is False
    # 1ページ作品は強調1コマが正当なので対象外。
    assert story.script_needs_repaneling({"pages": [{"page": 1, "panels": [one()]}]}) is False


def test_grid_layout_produces_valid_bboxes() -> None:
    assert story.distribute_rows(6) == [3, 3]
    assert story.distribute_rows(5) == [3, 2]
    assert story.distribute_rows(7) == [3, 2, 2]
    for count in range(1, 10):
        boxes = story.grid_layout(count)
        assert len(boxes) == count
        for left, top, width, height in boxes:
            assert width > 0 and height > 0
            assert left >= 0 and top >= 0
            assert left + width <= 1.0001 and top + height <= 1.0001


def test_script_to_manga_supports_six_panels() -> None:

    base = MangaProject(title="本")
    panels = [{"shot": "medium shot", "dialogue": []} for _ in range(6)]
    script = ScriptStage.model_validate({"pages": [{"page": 1, "panels": panels}]})
    manga = story.script_to_manga(script, base)
    page = manga.pages[0]
    assert len(page.panels) == 6
    # bboxはPanel構築時に範囲検証される。一意なpanel_idも確認する。
    assert len({panel.panel_id for panel in page.panels}) == 6


def test_silent_panel_keeps_characters() -> None:

    base = MangaProject(
        title="本",
        characters=[
            Character(
                id="mika",
                display_name="城ヶ崎美嘉",
                aliases=["美嘉"],
                trigger_prompt="jougasaki mika",
            ),
        ],
    )
    script = ScriptStage.model_validate(
        {
            "pages": [
                {
                    "page": 1,
                    "panels": [
                        # 台詞なしだがcharacters明記 → 解決される
                        {"shot": "wide", "characters": ["美嘉"], "dialogue": []},
                        # 台詞もcharactersも無い → ページ構成のフォールバックで解決される
                        {"shot": "close", "characters": [], "dialogue": []},
                    ],
                }
            ]
        }
    )
    manga = story.script_to_manga(script, base, page_characters={1: ["城ヶ崎美嘉"]})
    panels = manga.pages[0].panels
    assert panels[0].characters == ["mika"], "characters明記の無言コマが紐づくこと"
    assert panels[1].characters == ["mika"], "ページ構成フォールバックで無言コマが紐づくこと"
