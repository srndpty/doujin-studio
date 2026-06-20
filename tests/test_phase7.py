from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import database, knowledge, story
from backend.app.config import Settings
from backend.app.database import StoryGenerationSessionRecord, create_session_factory
from backend.app.llm import LLMError, OpenAICompatibleClient
from backend.app.main import create_app


def make_client(
    tmp_path: Path, llm_provider: str = "stub", knowledge_dir: Path | None = None
) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        export_dir=tmp_path / "exports",
        knowledge_dir=knowledge_dir or tmp_path / "knowledge",
        image_backend="stub",
        llm_provider=llm_provider,
    )
    return TestClient(create_app(settings))


def make_session(tmp_path: Path):
    factory = create_session_factory(f"sqlite:///{tmp_path / 'knowledge.db'}")
    return factory()


def create_project(client: TestClient, work_name: str = "テスト作品", target_pages: int = 4) -> str:
    response = client.post("/api/projects", json={"title": "本", "work_name": work_name, "target_pages": target_pages})
    project_id = response.json()["id"]
    client.post(
        f"/api/projects/{project_id}/generate-name",
        json={
            "work_name": work_name,
            "character_a": "春香",
            "character_b": "千早",
            "situation": "事務所で相談する",
            "ending_direction": "笑って終わる",
            "target_pages": target_pages,
        },
    )
    return project_id


# --- 知識DBとチャンク分割 ---

def test_markdown_txt_json_chunking() -> None:
    md = knowledge.chunk_document("markdown", "# 見出しA\n本文A\n## 見出しB\n本文B", "doc")
    assert [chunk.title for chunk in md] == ["見出しA", "見出しB"]
    assert md[0].content == "本文A"

    txt = knowledge.chunk_document("txt", "あ" * 1700, "メモ")
    assert len(txt) == 3

    payload = json.dumps(
        [{"kind": "character", "title": "春香", "content": "明るい少女", "policy": "口調維持", "tags": ["主役"]}],
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
        content=json.dumps({"kind": "character", "title": "凛", "content": "クールな後輩"}, ensure_ascii=False),
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
    required = knowledge.import_source(
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


# --- スタブによる段階生成フロー ---

def run_full_stub_flow(client: TestClient, project_id: str, target_pages: int) -> str:
    session = client.post(
        f"/api/projects/{project_id}/story-sessions",
        json={"target_pages": target_pages, "instruction": "短い日常話"},
    ).json()
    session_id = session["id"]
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
            f"/api/projects/{project_id}/story-sessions", json={"target_pages": 4}
        ).json()
        session_id = session["id"]

        # brief未承認でplotは生成できない。
        blocked = client.post(f"/api/story-sessions/{session_id}/stages/plot/generate")
        assert blocked.status_code == 400

        for stage in ["brief", "plot", "pages", "script"]:
            client.post(f"/api/story-sessions/{session_id}/stages/{stage}/generate")
            client.post(f"/api/story-sessions/{session_id}/stages/{stage}/approve")

        # 上流briefを編集すると下流が未承認へ戻る。
        current = client.get(f"/api/story-sessions/{session_id}").json()
        brief_data = current["stages"]["brief"]["data"]
        brief_data["tone"] = "しっとり"
        edited = client.put(
            f"/api/story-sessions/{session_id}/stages/brief", json={"data": brief_data}
        ).json()
        assert edited["stages"]["brief"]["status"] == "draft"
        assert edited["stages"]["plot"]["status"] == "draft"
        assert edited["stages"]["script"]["status"] == "draft"


def test_apply_and_revision_roundtrip(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        before = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        first_theme = before["pages"][0]["theme"]

        session_id = run_full_stub_flow(client, project_id, 4)
        applied = client.post(f"/api/story-sessions/{session_id}/apply")
        assert applied.status_code == 200
        manga = applied.json()["manga_json"]
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

        restored = client.post(
            f"/api/projects/{project_id}/revisions/{revisions[0]['id']}/restore"
        ).json()
        assert restored["manga_json"]["pages"][0]["theme"] == first_theme
        # 復元時も現在状態を新リビジョンとして保存する。
        assert len(client.get(f"/api/projects/{project_id}/revisions").json()) == 2


def test_required_knowledge_recorded_in_stage(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post(
            "/api/knowledge/documents",
            json={
                "work_name": "知識作品",
                "title": "必須",
                "doc_type": "json",
                "usage": "required",
                "content": json.dumps({"kind": "character", "title": "ヒロイン", "content": "芯の強い少女"}, ensure_ascii=False),
            },
        )
        project_id = create_project(client, work_name="知識作品")
        session = client.post(
            f"/api/projects/{project_id}/story-sessions", json={"work_name": "知識作品", "target_pages": 4}
        ).json()
        session_id = session["id"]
        generated = client.post(f"/api/story-sessions/{session_id}/stages/brief/generate").json()
        assert generated["stages"]["brief"]["knowledge_ids"], "required知識がステージに記録されること"
        # スタブはcharacter種別の知識を登場人物へ反映する。
        names = [c["name"] for c in generated["stages"]["brief"]["data"]["characters"]]
        assert "ヒロイン" in names


# --- 知識API ---

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
        assert len(client.get("/api/knowledge/sources", params={"work_name": "API作品"}).json()) == 1


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
            f"/api/projects/{project_id}/story-sessions",
            json={
                "knowledge_work_id": "local-work",
                "target_pages": 4,
                "instruction": "美嘉の日常",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["work_name"] == "ローカル作品"
        assert client.get(f"/api/projects/{project_id}").json()["work_name"] == "ローカル作品"

        generated = client.post(
            f"/api/story-sessions/{created.json()['id']}/stages/brief/generate"
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
            f"/api/projects/{project_id}/story-sessions",
            json={"knowledge_work_id": "local-work", "target_pages": 4},
        )
        assert second.status_code == 200
        sources = client.get(
            "/api/knowledge/sources", params={"work_name": "ローカル作品"}
        ).json()
        assert len(sources) == 1
        search = client.post(
            "/api/knowledge/search",
            json={"work_name": "ローカル作品", "query": "莉嘉"},
        ).json()
        assert search["hits"]


def test_llm_status_stub(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        status = client.get("/api/llm/status").json()
        assert status["provider"] == "stub"
        assert status["connected"] is True


# --- OpenAI互換LLM経路 ---

VALID_BRIEF = json.dumps(
    {
        "synopsis": "海辺の町の短い物語",
        "tone": "穏やか",
        "characters": [{"name": "海斗", "role": "主役"}],
        "canon_conditions": ["原作の地名を守る"],
    },
    ensure_ascii=False,
)


class FakeLLM:
    provider = "openai_compatible"

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list = []

    async def chat(self, messages: list[dict], want_json: bool = True) -> str:
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def generate_brief_with(tmp_path: Path, llm) -> dict:
    factory = create_session_factory(f"sqlite:///{tmp_path / 'story.db'}")
    settings = Settings()
    with factory() as session:
        record = story.create_session(
            session, project_id="p1", work_name="作品", target_pages=4, instruction="日常"
        )
        asyncio.run(story.generate_stage(session, llm, settings, record, "brief"))
        return story.load_stages(record)["brief"]


def test_llm_normal_response(tmp_path: Path) -> None:
    stage = generate_brief_with(tmp_path, FakeLLM([VALID_BRIEF]))
    assert stage["status"] == "draft"
    assert stage["error"] is None
    assert stage["data"]["synopsis"] == "海辺の町の短い物語"


def test_llm_invalid_json_is_corrected_once(tmp_path: Path) -> None:
    llm = FakeLLM(["これはJSONではありません", VALID_BRIEF])
    stage = generate_brief_with(tmp_path, llm)
    assert stage["status"] == "draft"
    assert stage["data"]["tone"] == "穏やか"
    assert len(llm.calls) == 2, "不正JSONは1回だけ修正要求する"


def test_llm_persistent_failure_records_error(tmp_path: Path) -> None:
    stage = generate_brief_with(tmp_path, FakeLLM(["壊れた", "また壊れた"]))
    assert stage["status"] == "empty"
    assert stage["error"]


def test_llm_timeout_and_connection_failure(tmp_path: Path) -> None:
    timeout = generate_brief_with(tmp_path, FakeLLM([LLMError("LLM応答がタイムアウトしました")]))
    assert timeout["status"] == "empty"
    assert "タイムアウト" in timeout["error"]

    connection = generate_brief_with(tmp_path, FakeLLM([LLMError("LLMへ接続できません")]))
    assert "接続できません" in connection["error"]


def test_openai_client_parses_and_handles_errors(monkeypatch) -> None:
    import httpx

    class MockResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class MockAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, json: dict) -> MockResponse:
            return MockResponse({"choices": [{"message": {"content": VALID_BRIEF}}]})

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    client = OpenAICompatibleClient("http://x/v1", "model", 5.0, "auto")
    content = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert "海斗" in content

    class AutoFallbackClient(MockAsyncClient):
        payloads: list[dict] = []

        async def post(self, url: str, json: dict) -> MockResponse:
            self.payloads.append(json)
            if len(self.payloads) == 1:
                response = MockResponse({"error": "json_objectは非対応"})
                response.status_code = 400
                return response
            response = MockResponse({"choices": [{"message": {"content": VALID_BRIEF}}]})
            response.status_code = 200
            return response

    monkeypatch.setattr(httpx, "AsyncClient", AutoFallbackClient)
    content = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert "海斗" in content
    assert "response_format" in AutoFallbackClient.payloads[0]
    assert "response_format" not in AutoFallbackClient.payloads[1]

    class TimeoutClient(MockAsyncClient):
        async def post(self, url: str, json: dict):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", TimeoutClient)
    try:
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
        assert False, "タイムアウトでLLMErrorになること"
    except LLMError as exc:
        assert "タイムアウト" in str(exc)
