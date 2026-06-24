from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
from json_repair import repair_json

from .config import Settings


@dataclass(frozen=True)
class LLMStatus:
    provider: str
    base_url: str
    model: str
    connected: bool
    available_models: list[str]
    message: str


class LLMError(RuntimeError):
    """LLM呼び出しに失敗したことを表す。"""


class StubLLMClient:
    """LLMを起動せずに段階生成を確認するためのスタブ。"""

    provider = "stub"

    async def chat(self, messages: list[dict], want_json: bool = True) -> str:
        # story.py側でスタブ生成を行うため、本メソッドは利用されない。
        return "{}"

    async def status(self) -> LLMStatus:
        return LLMStatus(
            provider="stub",
            base_url="",
            model="stub",
            connected=True,
            available_models=["stub"],
            message="スタブLLMを使用します。LLMなしで全工程を確認できます",
        )


class UnavailableLLMClient:
    """外部LLMを使えないと確定したときのクライアント（接続不能・指定モデル未ロード等）。

    明示的にopenai_compatibleを要求した利用者へ黙ってstub生成を返さないため、chat()は
    保存済みの理由を含むLLMErrorを必ず送出する。statusも未接続として理由を残す。
    """

    provider = "openai_compatible"

    def __init__(self, base_url: str, model: str, reason: str) -> None:
        self.base_url = base_url
        self.model = model
        self.reason = reason

    async def chat(self, messages: list[dict], want_json: bool = True) -> str:
        raise LLMError(self.reason)

    async def status(self) -> LLMStatus:
        return LLMStatus(
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
            connected=False,
            available_models=[],
            message=self.reason,
        )


class OpenAICompatibleClient:
    """OpenAI互換のchat completions APIへ接続するクライアント。"""

    provider = "openai_compatible"

    def __init__(self, base_url: str, model: str, timeout_seconds: float, json_mode: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.json_mode = json_mode

    async def chat(self, messages: list[dict], want_json: bool = True) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "stream": False,
        }
        if want_json and self.json_mode in {"auto", "response_format"}:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=payload)
                if (
                    self.json_mode == "auto"
                    and "response_format" in payload
                    and getattr(response, "status_code", 200) == 400
                ):
                    fallback_payload = dict(payload)
                    fallback_payload.pop("response_format", None)
                    response = await client.post(
                        f"{self.base_url}/chat/completions", json=fallback_payload
                    )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM応答がタイムアウトしました: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLMがエラーを返しました: {exc.response.status_code} {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLMへ接続できません: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM応答の形式が不正です: {json.dumps(data)[:200]}") from exc

    async def status(self) -> LLMStatus:
        connected = False
        models: list[str] = []
        message = ""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/models")
                response.raise_for_status()
                connected = True
                payload = response.json()
                models = [item.get("id", "") for item in payload.get("data", []) if item.get("id")]
                message = "LLMへ接続できました"
        except Exception as exc:
            message = f"LLMへ接続できません: {exc}"
        if connected and not self.model:
            message += " / LLM_MODELが未設定です"
        return LLMStatus(
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
            connected=connected,
            available_models=models,
            message=message,
        )


def build_llm_client(settings: Settings):
    if settings.llm_provider.lower() == "openai_compatible":
        return OpenAICompatibleClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            json_mode=settings.llm_json_mode,
        )
    return StubLLMClient()


def _select_model(settings: Settings, status: LLMStatus) -> tuple[str | None, str]:
    """接続済み前提で採用モデルを決める。(モデル or None=stub退避相当, 理由) を返す。

    LLM_MODELが非空なら厳密一致を必須にする。typo・アンロード・入替で勝手に別モデルへ
    切り替えると、品質・文体・JSON追従性が予告なく変わるため、不一致はstub退避/明示失敗にする。
    LLM_MODELが空のときだけREADMEどおりロード済み先頭を採用する。
    """
    if settings.llm_model:
        if settings.llm_model in status.available_models:
            return settings.llm_model, status.message
        loaded = ", ".join(status.available_models) or "なし"
        return None, f"指定モデル {settings.llm_model} が未ロードです（ロード済み: {loaded}）"
    if status.available_models:
        return status.available_models[0], status.message
    return None, "ロード済みモデルがありません"


async def resolve_llm_client(
    settings: Settings,
) -> StubLLMClient | OpenAICompatibleClient | UnavailableLLMClient:
    """現在ロードされているモデルを解決する。

    auto（またはmodel未指定のopenai_compatible）はリクエストごとに/modelsを確認するため、
    バックエンド再起動なしでLM Studioのモデルロードを反映できる。
    """
    provider = settings.llm_provider.lower()
    if provider not in {"auto", "openai_compatible"}:
        return StubLLMClient()
    probe = OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        json_mode=settings.llm_json_mode,
    )
    status = await probe.status()
    if not status.connected:
        # 明示的に外部LLMを要求したopenai_compatibleは、LLM_MODELの有無に関わらず
        # stubへ落とさず、生成を必ず失敗させる。stub退避はauto専用。
        if provider == "openai_compatible":
            return UnavailableLLMClient(settings.llm_base_url, settings.llm_model, status.message)
        return StubLLMClient()
    model, reason = _select_model(settings, status)
    if model is None:
        # 指定モデル未ロード/ロード済みなし。openai_compatibleは空modelで誤ったモデルへ
        # 流れないよう失敗専用clientを返し、autoはstubへ退避する。
        if provider == "openai_compatible":
            return UnavailableLLMClient(settings.llm_base_url, settings.llm_model, reason)
        return StubLLMClient()
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
        json_mode=settings.llm_json_mode,
    )


async def get_llm_status(settings: Settings) -> LLMStatus:
    provider = settings.llm_provider.lower()
    if provider not in {"auto", "openai_compatible"}:
        return await StubLLMClient().status()
    probe = OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        json_mode=settings.llm_json_mode,
    )
    status = await probe.status()
    if not status.connected:
        # 接続エラーの原因をそのまま残す（「ロード済みモデルがない」で潰さない）。
        if provider == "auto":
            return LLMStatus(
                provider="stub",
                base_url=settings.llm_base_url,
                model="stub",
                connected=False,
                available_models=[],
                message=f"{status.message} / スタブLLMを使用します。LLMなしで全工程を確認できます",
            )
        return status
    model, reason = _select_model(settings, status)
    if model is None:
        if provider == "auto":
            return LLMStatus(
                provider="stub",
                base_url=settings.llm_base_url,
                model="stub",
                connected=False,
                available_models=status.available_models,
                message=f"{reason} / スタブLLMを使用します。LLMなしで全工程を確認できます",
            )
        # openai_compatibleは生成が必ず失敗する。通信は到達していてもUIの正常判定
        # (provider!=="stub" && connected)を緑にしないよう、connected=Falseで返す。
        return LLMStatus(
            provider=probe.provider,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            connected=False,
            available_models=status.available_models,
            message=reason,
        )
    return LLMStatus(
        provider=probe.provider,
        base_url=settings.llm_base_url,
        model=model,
        connected=status.connected,
        available_models=status.available_models,
        message=reason,
    )


def extract_json_object(content: str) -> dict:
    """LLM出力からJSONオブジェクトを抽出する。前後の地の文やコードフェンスを許容する。"""
    text = content.strip()
    if text.startswith("```"):
        # ```json ... ``` のフェンスを除去する。
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    candidate = text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as original_error:
        # ローカルLLMの長い出力では、内容が揃っていても配列要素間のカンマなどが
        # 抜けることがある。再問い合わせ前に決定的な構文修復を行い、その後の
        # Pydantic検証でスキーマ・ページ数・型を必ず再検査する。
        try:
            parsed = repair_json(candidate, return_objects=True)
        except Exception:
            raise original_error from None
    if not isinstance(parsed, dict):
        raise ValueError("LLM応答はJSONオブジェクトにしてください")
    return parsed
