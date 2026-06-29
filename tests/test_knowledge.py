"""知識DBと知識連携のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app import database, knowledge, story
from backend.app.config import Settings
from backend.app.database import (
    create_session_factory,
)
from backend.app.schemas import (
    Character,
)
from backend.app.story_characters import _is_weak_trigger
from tests.helpers import (
    create_stub_project as create_project,
)
from tests.helpers import (
    make_stub_client as make_client,
)
from tests.helpers import (
    mutation_url,
)


def make_session(tmp_path: Path):
    factory = create_session_factory(f"sqlite:///{tmp_path / 'knowledge.db'}")
    return factory()


def test_markdown_txt_json_chunking() -> None:
    md = knowledge.chunk_document("markdown", "# 見出しA\n本文A\n## 見出しB\n本文B", "doc")
    assert [chunk.title for chunk in md] == ["見出しA", "見出しB"]
    assert md[0].content == "本文A"

    txt = knowledge.chunk_document("txt", "あ" * 1700, "メモ")
    assert len(txt) == 3

    payload = json.dumps(
        [
            {
                "kind": "character",
                "title": "春香",
                "content": "明るい少女",
                "policy": "口調維持",
                "tags": ["主役"],
            }
        ],
        ensure_ascii=False,
    )
    js = knowledge.chunk_document("json", payload, "設定")
    assert js[0].kind == "character"
    assert js[0].title == "春香"
    assert js[0].tags == ["主役"]


def test_trigram_and_short_like_search(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    knowledge.import_source(
        session,
        work_name="作品X",
        title="設定",
        doc_type="txt",
        usage="reference",
        content="放課後の音楽室で先輩と後輩が出会う長い物語の説明文。",
    )
    knowledge.import_source(
        session,
        work_name="作品X",
        title="キャラ",
        doc_type="json",
        usage="reference",
        content=json.dumps(
            {"kind": "character", "title": "凛", "content": "クールな後輩"}, ensure_ascii=False
        ),
    )

    hits = knowledge.search_chunks(session, work_name="作品X", query="音楽室", limit=5)
    assert hits, "3文字以上の語が検索できること"
    if database.FTS5_AVAILABLE:
        assert hits[0][2] == "trigram"

    short = knowledge.search_chunks(session, work_name="作品X", query="凛", limit=5)
    assert short, "短いキャラ名がLIKEで補完されること"
    assert short[0][2] == "like"


def test_required_always_in_context_reference_by_relevance(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    knowledge.import_source(
        session,
        work_name="作品Y",
        title="必須設定",
        doc_type="txt",
        usage="required",
        content="この世界では魔法の使用が固く禁止されている。",
    )
    knowledge.import_source(
        session,
        work_name="作品Y",
        title="参考A",
        doc_type="txt",
        usage="reference",
        content="登場人物は放課後によく図書館へ立ち寄る。",
    )
    settings = Settings()
    context = story.build_context(session, settings, "作品Y", "図書館")
    required_ids = [chunk.id for chunk in knowledge.get_required_chunks(session, "作品Y")]
    assert all(rid in context.knowledge_ids for rid in required_ids)
    assert "魔法の使用が固く禁止" in context.required_text
    assert "図書館" in context.reference_text


def test_required_knowledge_recorded_in_stage(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post(
            "/api/knowledge/documents",
            json={
                "work_name": "知識作品",
                "title": "必須",
                "doc_type": "json",
                "usage": "required",
                "content": json.dumps(
                    {"kind": "character", "title": "ヒロイン", "content": "芯の強い少女"},
                    ensure_ascii=False,
                ),
            },
        )
        project_id = create_project(client, work_name="知識作品")
        session = client.post(
            mutation_url(client, project_id, "story-sessions"),
            json={"work_name": "知識作品", "target_pages": 4},
        ).json()
        session_id = session["result"]["id"]
        generated = client.post(f"/api/story-sessions/{session_id}/stages/brief/generate").json()
        assert generated["stages"]["brief"]["knowledge_ids"], (
            "required知識がステージに記録されること"
        )
        # スタブはcharacter種別の知識を登場人物へ反映する。
        names = [c["name"] for c in generated["stages"]["brief"]["data"]["characters"]]
        assert "ヒロイン" in names


def test_knowledge_import_and_search_api(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/knowledge/sources/import",
            json={
                "work_name": "API作品",
                "usage": "reference",
                "files": [
                    {"filename": "world.md", "content": "# 世界観\n海辺の小さな町が舞台。"},
                    {"filename": "notes.txt", "content": "主人公は釣りが得意。"},
                ],
            },
        )
        assert response.status_code == 200
        assert len(response.json()["sources"]) == 2

        sources = client.get("/api/knowledge/sources", params={"work_name": "API作品"}).json()
        assert len(sources) == 2

        search = client.post(
            "/api/knowledge/search", json={"work_name": "API作品", "query": "海辺", "limit": 5}
        ).json()
        assert any("海辺" in hit["chunk"]["content"] for hit in search["hits"])

        delete = client.delete(f"/api/knowledge/sources/{sources[0]['id']}")
        assert delete.status_code == 200
        assert (
            len(client.get("/api/knowledge/sources", params={"work_name": "API作品"}).json()) == 1
        )


def test_local_knowledge_pack_is_selected_and_synced(tmp_path: Path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    pack_dir = knowledge_dir / "local-work"
    pack_dir.mkdir(parents=True)
    (pack_dir / "work.json").write_text(
        json.dumps(
            {
                "work_id": "local-work",
                "work_name": "ローカル作品",
                "description": "テスト用",
                "documents": [{"file": "required.json", "usage": "required"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pack_dir / "required.json").write_text(
        json.dumps(
            [{"kind": "character", "title": "美嘉", "content": "面倒見のよい主人公"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with make_client(tmp_path, knowledge_dir=knowledge_dir) as client:
        works = client.get("/api/knowledge/local-works")
        assert works.status_code == 200
        assert works.json()[0]["work_id"] == "local-work"

        project_id = create_project(client, work_name="変更前")
        created = client.post(
            mutation_url(client, project_id, "story-sessions"),
            json={
                "knowledge_work_id": "local-work",
                "target_pages": 4,
                "instruction": "美嘉の日常",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["result"]["work_name"] == "ローカル作品"
        assert client.get(f"/api/projects/{project_id}").json()["work_name"] == "ローカル作品"

        generated = client.post(
            f"/api/story-sessions/{created.json()['result']['id']}/stages/brief/generate"
        ).json()
        assert generated["stages"]["brief"]["knowledge_ids"]
        assert generated["stages"]["brief"]["data"]["characters"][0]["name"] == "美嘉"

        # ファイル修正後の新規セッションでは同じローカルソースを置換する。
        (pack_dir / "required.json").write_text(
            json.dumps(
                [{"kind": "character", "title": "莉嘉", "content": "明るい妹"}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        second = client.post(
            mutation_url(client, project_id, "story-sessions"),
            json={"knowledge_work_id": "local-work", "target_pages": 4},
        )
        assert second.status_code == 200
        sources = client.get("/api/knowledge/sources", params={"work_name": "ローカル作品"}).json()
        assert len(sources) == 1
        search = client.post(
            "/api/knowledge/search",
            json={"work_name": "ローカル作品", "query": "莉嘉"},
        ).json()
        assert search["hits"]


def test_knowledge_character_image_prompt_applied(tmp_path: Path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    pack_dir = knowledge_dir / "cg"
    pack_dir.mkdir(parents=True)
    (pack_dir / "work.json").write_text(
        json.dumps(
            {
                "work_id": "cg",
                "work_name": "シンデレラ",
                "documents": [{"file": "required.json", "usage": "required"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pack_dir / "required.json").write_text(
        json.dumps(
            [
                {
                    "kind": "character",
                    "title": "城ヶ崎美嘉",
                    "content": "面倒見のよい姉",
                    "image": {
                        "id": "jougasaki_mika",
                        "aliases": ["美嘉"],
                        "trigger_prompt": "jougasaki mika, idolmaster cinderella girls",
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with make_client(tmp_path, knowledge_dir=knowledge_dir) as client:
        project_id = create_project(client, work_name="変更前")
        session_id = client.post(
            f"/api/projects/{project_id}/story-sessions?revision="
            f"{client.get(f'/api/projects/{project_id}').json()['revision']}",
            json={"knowledge_work_id": "cg", "target_pages": 4, "instruction": "美嘉の日常"},
        ).json()["result"]["id"]
        for stage in ["brief", "plot", "pages", "script"]:
            assert (
                client.post(f"/api/story-sessions/{session_id}/stages/{stage}/generate").status_code
                == 200
            )
            assert (
                client.post(f"/api/story-sessions/{session_id}/stages/{stage}/approve").status_code
                == 200
            )

        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        manga = client.post(f"/api/story-sessions/{session_id}/apply?revision={revision}").json()[
            "project"
        ]["manga_json"]

        mika = next((c for c in manga["characters"] if c["display_name"] == "城ヶ崎美嘉"), None)
        assert mika is not None, "知識のキャラがプロジェクトへ反映されること"
        assert "jougasaki mika" in mika["trigger_prompt"]

        # 美嘉が話すコマには美嘉のキャラIDが紐づくこと。
        linked = [
            panel
            for page in manga["pages"]
            for panel in page["panels"]
            if mika["id"] in panel["characters"]
        ]
        assert linked, "話者解決で美嘉がコマへ紐づくこと"

        # 合成プロンプトにtriggerが含まれること。
        preview = client.get(
            f"/api/projects/{project_id}/panels/{linked[0]['panel_id']}/prompt-preview"
        ).json()
        assert "jougasaki mika" in preview["positive_prompt"]


def test_is_weak_trigger_treats_japanese_only_as_weak() -> None:

    japanese = Character(id="rika", display_name="城ヶ崎莉嘉", trigger_prompt="城ヶ崎莉嘉")
    booru = Character(
        id="rika",
        display_name="城ヶ崎莉嘉",
        trigger_prompt="jougasaki rika, idolmaster cinderella girls",
    )
    # 日本語名そのまま・表示名一致はいずれも弱trigger（素モデルが解釈できない）。
    assert _is_weak_trigger(japanese) is True
    assert _is_weak_trigger(booru) is False


def test_duplicate_character_merge_keeps_existing_profile(tmp_path: Path) -> None:
    with make_session(tmp_path) as session:
        weak = {
            "kind": "character",
            "title": "城ヶ崎莉嘉",
            "content": "詳細プロフィール",
            "image": {
                "id": "rika",
                "aliases": ["莉嘉"],
                "trigger_prompt": "城ヶ崎莉嘉",
                "appearance_prompt": "blonde side ponytail",
                "outfit_prompt": "pink idol costume",
                "negative_prompt": "bad hands",
                "lora_node_id": "lora-1",
                "lora_name": "rika.safetensors",
                "speech_style": "元気",
            },
        }
        strong = {
            "kind": "character",
            "title": "城ヶ崎莉嘉",
            "content": "強いtriggerだけを持つ行",
            "image": {
                "id": "rika_tag",
                "aliases": ["Rika"],
                "trigger_prompt": "jougasaki rika, idolmaster cinderella girls",
            },
        }
        knowledge.import_source(
            session,
            work_name="シンデレラ",
            title="characters",
            doc_type="json",
            usage="required",
            content=json.dumps([weak, strong], ensure_ascii=False),
        )
        session.commit()

        chunks = knowledge.get_character_chunks(session, "シンデレラ")
        assert len(chunks) == 1
        assert "詳細プロフィール" in chunks[0].content

        characters = story.build_characters_from_knowledge(session, "シンデレラ")

    assert len(characters) == 1
    rika = characters[0]
    assert rika.id == "rika"
    assert "jougasaki rika" in rika.trigger_prompt
    assert rika.aliases == ["莉嘉", "Rika"]
    assert rika.appearance_prompt == "blonde side ponytail"
    assert rika.outfit_prompt == "pink idol costume"
    assert rika.negative_prompt == "bad hands"
    assert rika.lora_name == "rika.safetensors"
    assert rika.speech_style == "元気"
