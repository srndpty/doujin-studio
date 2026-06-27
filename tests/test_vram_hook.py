"""LLMステージ前のComfyUI VRAM解放フックのテスト。"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx

from backend.app import story
from backend.app.config import Settings


class _FakeResponse:
    def __init__(self, status_code: int, calls: list) -> None:
        self.status_code = status_code
        self._calls = calls

    def raise_for_status(self) -> None:
        self._calls.append(("raise_for_status", self.status_code))
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "http://x/free"),
                response=httpx.Response(self.status_code),
            )


class _RecordingClient:
    def __init__(self, calls: list, status: int = 200, raise_conn: bool = False) -> None:
        self._calls = calls
        self._status = status
        self._raise_conn = raise_conn

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url: str, json=None) -> _FakeResponse:
        self._calls.append((url, json))
        if self._raise_conn:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(self._status, self._calls)


# --- free_comfyui_vram 単体 ---


def test_free_posts_and_checks_status(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        story.httpx, "AsyncClient", lambda *a, **k: _RecordingClient(calls, status=200)
    )
    asyncio.run(story.free_comfyui_vram(Settings(comfyui_base_url="http://comfy:8188")))
    assert calls[0] == ("http://comfy:8188/free", {"unload_models": True, "free_memory": True})
    # 成功時も raise_for_status で状態を検査する。
    assert ("raise_for_status", 200) in calls


def test_free_swallows_http_errors_and_logs(monkeypatch, caplog) -> None:
    # 404/500 は httpx では例外にならないが raise_for_status で検知し、外へは出さず debug ログに残す。
    for status in (404, 500):
        monkeypatch.setattr(
            story.httpx, "AsyncClient", lambda *a, s=status, **k: _RecordingClient([], status=s)
        )
        with caplog.at_level(logging.DEBUG, logger="backend.app.story"):
            asyncio.run(story.free_comfyui_vram(Settings(comfyui_base_url="http://comfy:8188")))
    assert any("/free" in record.getMessage() for record in caplog.records)


def test_free_swallows_connection_error(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        story.httpx, "AsyncClient", lambda *a, **k: _RecordingClient(calls, raise_conn=True)
    )
    asyncio.run(story.free_comfyui_vram(Settings(comfyui_base_url="http://comfy:8188/")))
    assert calls and calls[0][0].endswith("/free")


# --- generate_stage との結合（フラグ配線・呼出順序） ---

_BRIEF = {"synopsis": "x", "tone": "", "characters": [], "canon_conditions": []}


def _make_record() -> SimpleNamespace:
    return SimpleNamespace(
        id="session-test",
        stages_json=json.dumps(story.empty_stages()),
        work_name="",
        instruction="",
        target_pages=4,
    )


def _patch_llm(monkeypatch, calls: list) -> None:
    monkeypatch.setattr(story, "save_stages", lambda *a, **k: None)

    async def fake_llm(*a, **k):
        calls.append("llm")
        return dict(_BRIEF)

    monkeypatch.setattr(story, "generate_llm_stage", fake_llm)


def test_generate_stage_frees_before_llm_when_enabled(monkeypatch) -> None:
    calls: list = []
    _patch_llm(monkeypatch, calls)

    async def fake_free(settings):
        calls.append("free")

    monkeypatch.setattr(story, "free_comfyui_vram", fake_free)
    settings = Settings(comfyui_free_before_llm=True)
    asyncio.run(story.generate_stage(None, object(), settings, _make_record(), "brief"))
    assert calls == ["free", "llm"]  # 実LLM経路では free が llm の前に一度呼ばれる


def test_generate_stage_skips_free_when_disabled(monkeypatch) -> None:
    calls: list = []
    _patch_llm(monkeypatch, calls)

    async def fake_free(settings):
        calls.append("free")

    monkeypatch.setattr(story, "free_comfyui_vram", fake_free)
    settings = Settings(comfyui_free_before_llm=False)
    asyncio.run(story.generate_stage(None, object(), settings, _make_record(), "brief"))
    assert calls == ["llm"]


def test_generate_stage_stub_does_not_free(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(story, "save_stages", lambda *a, **k: None)
    monkeypatch.setattr(story, "generate_stub_stage", lambda *a, **k: dict(_BRIEF))

    async def fake_free(settings):
        calls.append("free")

    monkeypatch.setattr(story, "free_comfyui_vram", fake_free)
    settings = Settings(comfyui_free_before_llm=True)
    asyncio.run(
        story.generate_stage(None, story.StubLLMClient(), settings, _make_record(), "brief")
    )
    assert "free" not in calls  # stub 経路では /free を叩かない


def test_generate_stage_continues_when_free_fails(monkeypatch) -> None:
    calls: list = []
    _patch_llm(monkeypatch, calls)
    # 実 free_comfyui_vram を使い、/free が HTTP 500 でも LLM 生成が継続することを確認。
    monkeypatch.setattr(
        story.httpx, "AsyncClient", lambda *a, **k: _RecordingClient([], status=500)
    )
    settings = Settings(comfyui_free_before_llm=True)
    record = _make_record()
    asyncio.run(story.generate_stage(None, object(), settings, record, "brief"))
    assert calls == ["llm"]  # free 失敗を握り潰し LLM へ進む
