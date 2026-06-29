"""schema互換性と整合性検証のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app import story
from backend.app.schemas import (
    Dialogue,
)
from tests.helpers import (
    create_stub_project as create_project,
)
from tests.helpers import (
    make_stub_client as make_client,
)


def test_balloon_values_are_migrated_from_old_schema() -> None:
    dialogue = Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "round"})
    assert dialogue.balloon == "oval"
    assert (
        Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "thought"}).balloon
        == "cloud"
    )
    assert (
        Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "shout"}).balloon
        == "burst"
    )
    # 新しい値はそのまま通る。
    assert (
        Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "caption"}).balloon
        == "caption"
    )


def test_validate_stage_accepts_bare_pages_array() -> None:
    # LLMが {"pages":[...]} のラッパーを省いて配列だけ返すケースを吸収する。
    bare = [
        {
            "page": 1,
            "panels": [{"shot": "wide", "dialogue": [{"speaker": "美嘉", "text": "やったね"}]}],
        }
    ]
    validated = story.validate_stage_data("script", bare, target_pages=1)
    assert validated["pages"][0]["page"] == 1
    assert validated["pages"][0]["panels"][0]["dialogue"][0]["speaker"] == "美嘉"


def test_script_numeric_fields_are_normalized() -> None:
    data = {
        "pages": [
            {
                "page": page,
                "layout": 1,
                "panels": [
                    {
                        "shot": 1,
                        "camera": 2,
                        "location": "控室",
                        "visual_prompt": "two idols talking",
                        "dialogue": [{"speaker": "美嘉", "text": 3}],
                        "sfx": 4,
                    }
                ],
            }
            for page in range(1, 5)
        ]
    }
    validated = story.validate_stage_data("script", data, 4)
    first = validated["pages"][0]
    assert first["layout"] == "1"
    assert first["panels"][0]["shot"] == "1"
    assert first["panels"][0]["camera"] == "2"
    assert first["panels"][0]["dialogue"][0]["text"] == "3"
    # 擬音は構造化（{text, style, position}）され、文字列・数値はtextへ正規化される。
    assert first["panels"][0]["sfx"][0]["text"] == "4"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["pages"].append({**m["pages"][0]}),  # ページ番号重複
        lambda m: m["pages"][0]["panels"].append({**m["pages"][0]["panels"][0]}),  # コマID重複
        lambda m: m["workflow_presets"].append({**m["workflow_presets"][0]}),  # preset ID重複
        lambda m: m.update({"active_workflow_preset_id": "ghost"}),  # 既定preset参照切れ
        lambda m: m["pages"][0]["panels"][0]["generation"].update(
            {"workflow_preset_id": "ghost"}
        ),  # コマのpreset参照切れ
    ],
)
def test_manga_consistency_rejects_structural_breakage(tmp_path: Path, mutate) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        mutate(manga)
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert response.status_code == 422
