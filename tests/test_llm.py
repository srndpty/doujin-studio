"""LLMとOpenAI互換クライアントのテスト。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import (
    make_stub_client as make_client,
)

from backend.app import story
from backend.app.config import Settings
from backend.app.database import (
    ProjectRecord,
    create_session_factory,
)
from backend.app.database import now_utc as db_now_utc
from backend.app.llm import LLMError, OpenAICompatibleClient, extract_json_object

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

    async def chat(self, messages: list[dict], want_json: bool = True, on_progress=None) -> str:
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_progress is not None:
            on_progress(item)
        return item


def generate_brief_with(tmp_path: Path, llm) -> dict:
    factory = create_session_factory(f"sqlite:///{tmp_path / 'story.db'}")
    settings = Settings()
    with factory() as session:
        # story_generation_sessions.project_idはprojectsへの外部キー。親を先に作る。
        # 同一tmp_pathで複数回呼ばれることがあるため重複作成を避ける。
        if session.get(ProjectRecord, "p1") is None:
            session.add(
                ProjectRecord(
                    id="p1",
                    title="t",
                    work_name="作品",
                    manga_json="{}",
                    created_at=db_now_utc(),
                    updated_at=db_now_utc(),
                )
            )
            session.commit()
        record = story.create_session(
            session, project_id="p1", work_name="作品", target_pages=4, instruction="日常"
        )
        asyncio.run(story.generate_stage(session, llm, settings, record, "brief"))
        return story.load_stages(record)["brief"]


def test_llm_status_stub(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        status = client.get("/api/llm/status").json()
        assert status["provider"] == "stub"
        assert status["connected"] is True


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


def test_llm_json_missing_comma_is_repaired_locally() -> None:
    parsed = extract_json_object(
        '{"pages": [{"page": 1, "panels": []}\n{"page": 2, "panels": []}]}'
    )
    assert [page["page"] for page in parsed["pages"]] == [1, 2]


def test_script_invalid_json_is_corrected_twice_with_previous_output(tmp_path: Path) -> None:
    valid_script = json.dumps(
        {
            "pages": [
                {
                    "page": page,
                    "layout": "dialogue",
                    "panels": [
                        {
                            "shot": "wide shot",
                            "camera": "eye level",
                            "location": "控室",
                            "visual_prompt": "two idols talking in a dressing room",
                            "characters": ["美嘉", "莉嘉"],
                            "dialogue": [{"speaker": "美嘉", "text": "準備できた？"}],
                            "sfx": [],
                        },
                        {
                            "shot": "close-up",
                            "camera": "eye level",
                            "location": "控室",
                            "visual_prompt": "cheerful younger sister smiling",
                            "characters": ["莉嘉"],
                            "dialogue": [{"speaker": "莉嘉", "text": "もちろん！"}],
                            "sfx": ["ぱっ"],
                        },
                    ],
                }
                for page in range(1, 5)
            ]
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(['{"pages": [', '{"pages" []}', valid_script])
    factory = create_session_factory(f"sqlite:///{tmp_path / 'script-retry.db'}")
    with factory() as session:
        session.add(
            ProjectRecord(
                id="p1",
                title="t",
                work_name="作品",
                manga_json="{}",
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
        record = story.create_session(
            session, project_id="p1", work_name="作品", target_pages=4, instruction="姉妹の日常"
        )
        result = asyncio.run(
            story.generate_llm_stage(
                llm,
                record,
                story.empty_stages(),
                "script",
                story.KnowledgeContext(),
                "",
            )
        )

    assert len(result["pages"]) == 4
    assert len(llm.calls) == 3
    assert any(message["role"] == "assistant" for message in llm.calls[1])
    assert llm.calls[2][-2]["content"] == '{"pages" []}'


def test_script_prompt_explains_shot_as_string() -> None:
    instruction = story.stage_instruction_text("script")
    assert "コマ番号ではなく" in instruction
    assert "文字列" in instruction


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


def test_openai_streaming_partial_failure_does_not_retry_blocking(monkeypatch) -> None:
    import httpx

    class BrokenStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"途中"}}]}'
            raise httpx.ReadError("切断")

    class MockAsyncClient:
        blocking_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        def stream(self, *args, **kwargs) -> BrokenStreamResponse:
            return BrokenStreamResponse()

        async def post(self, *args, **kwargs):
            self.blocking_calls += 1
            raise AssertionError("途中失敗時にblockingへ再送してはいけない")

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    client = OpenAICompatibleClient("http://x/v1", "model", 5.0, "auto")
    progress: list[str] = []
    try:
        asyncio.run(client.chat([{"role": "user", "content": "hi"}], on_progress=progress.append))
        assert False, "途中切断はLLMErrorになること"
    except LLMError as exc:
        assert "途中で切断" in str(exc)
    assert progress == ["途中"]
    assert MockAsyncClient.blocking_calls == 0


def test_openai_streaming_400_auto_falls_back_to_blocking(monkeypatch) -> None:
    import httpx

    class StreamRejectedResponse:
        status_code = 400
        text = "response_format is not supported"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def aread(self) -> bytes:
            return self.text.encode()

        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "http://x/v1/chat/completions")
            response = httpx.Response(400, text=self.text, request=request)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    class BlockingResponse:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": VALID_BRIEF}}]}

    class MockAsyncClient:
        payloads: list[dict] = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        def stream(self, *args, **kwargs) -> StreamRejectedResponse:
            return StreamRejectedResponse()

        async def post(self, url: str, json: dict) -> BlockingResponse:
            self.payloads.append(json)
            return BlockingResponse()

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    client = OpenAICompatibleClient("http://x/v1", "model", 5.0, "auto")
    content = asyncio.run(
        client.chat([{"role": "user", "content": "hi"}], on_progress=lambda _text: None)
    )

    assert "海斗" in content
    assert len(MockAsyncClient.payloads) == 1
    assert MockAsyncClient.payloads[0]["stream"] is False
