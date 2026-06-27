"""LLMステージ前のComfyUI VRAM解放フックのテスト。"""

from __future__ import annotations

import asyncio

from backend.app import story
from backend.app.config import Settings


class _RecordingClient:
    def __init__(self, calls: list, raise_error: bool = False) -> None:
        self._calls = calls
        self._raise = raise_error

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url: str, json=None) -> None:
        self._calls.append((url, json))
        if self._raise:
            raise RuntimeError("connection refused")


def test_free_comfyui_vram_posts_to_free(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(story.httpx, "AsyncClient", lambda *a, **k: _RecordingClient(calls))
    asyncio.run(story.free_comfyui_vram(Settings(comfyui_base_url="http://comfy:8188")))
    assert calls == [("http://comfy:8188/free", {"unload_models": True, "free_memory": True})]


def test_free_comfyui_vram_swallows_errors(monkeypatch) -> None:
    # 接続不可でも例外を投げず生成を止めない（ベストエフォート）。
    calls: list = []
    monkeypatch.setattr(
        story.httpx, "AsyncClient", lambda *a, **k: _RecordingClient(calls, raise_error=True)
    )
    asyncio.run(story.free_comfyui_vram(Settings(comfyui_base_url="http://comfy:8188/")))
    assert calls  # 呼び出しは行われたが例外は伝播しない
