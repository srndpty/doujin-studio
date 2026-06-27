from __future__ import annotations

import copy
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from . import knowledge, layout_engine
from .config import Settings
from .database import KnowledgeChunkRecord, StoryGenerationSessionRecord, now_utc
from .generator import (
    DEFAULT_COMMON_NEGATIVE_PROMPT,
    DEFAULT_COMMON_POSITIVE_PROMPT,
    LAYOUTS,
    compute_generation_size,
)
from .llm import LLMError, StubLLMClient, extract_json_object
from .schemas import (
    BriefCharacter,
    BriefStage,
    Character,
    CharacterArc,
    Dialogue,
    GenerationInfo,
    MangaProject,
    Page,
    PageOutline,
    PagesStage,
    Panel,
    PanelCharacter,
    PlotStage,
    ScriptCharacter,
    ScriptPanel,
    ScriptStage,
    Sfx,
    StorySessionResponse,
    StoryStageState,
)

STAGE_ORDER = ["brief", "plot", "pages", "script"]
STAGE_MODELS: dict[str, type[BaseModel]] = {
    "brief": BriefStage,
    "plot": PlotStage,
    "pages": PagesStage,
    "script": ScriptStage,
}

# コマ数とlayout hintから割り当てる既存レイアウト（bboxはサーバーが決める）。
PANEL_LAYOUTS: dict[int, dict[str, list[tuple[float, float, float, float]]]] = {
    1: {"default": [(0.06, 0.05, 0.88, 0.9)]},
    2: {
        "default": [(0.06, 0.05, 0.88, 0.43), (0.06, 0.52, 0.88, 0.43)],
        "horizontal": [(0.06, 0.05, 0.42, 0.9), (0.52, 0.05, 0.42, 0.9)],
    },
    3: {
        "default": LAYOUTS["reaction_3"],
        "punchline": LAYOUTS["punchline_3"],
    },
    4: {
        "default": LAYOUTS["conversation_4"],
        "vertical": LAYOUTS["vertical_3_start"],
    },
}


class StoryError(Exception):
    """段階生成の前提条件違反などのユーザー向けエラー。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- ステージ状態の入出力 ---


def empty_stages() -> dict:
    return {
        name: {
            "status": "empty",
            "data": None,
            "knowledge_ids": [],
            "error": None,
            "warnings": [],
            "updated_at": None,
        }
        for name in STAGE_ORDER
    }


def load_stages(record: StoryGenerationSessionRecord) -> dict:
    try:
        stages = json.loads(record.stages_json) if record.stages_json else {}
    except json.JSONDecodeError:
        stages = {}
    base = empty_stages()
    for name in STAGE_ORDER:
        if name in stages and isinstance(stages[name], dict):
            base[name].update(stages[name])
    return base


def save_stages(session: Session, record: StoryGenerationSessionRecord, stages: dict) -> None:
    record.stages_json = json.dumps(stages, ensure_ascii=False)
    record.updated_at = now_utc()
    session.commit()
    session.refresh(record)


def session_to_response(record: StoryGenerationSessionRecord) -> StorySessionResponse:
    stages = load_stages(record)
    return StorySessionResponse(
        id=record.id,
        project_id=record.project_id,
        work_name=record.work_name,
        target_pages=record.target_pages,
        instruction=record.instruction,
        stages={name: StoryStageState.model_validate(stages[name]) for name in STAGE_ORDER},
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def create_session(
    session: Session,
    *,
    project_id: str,
    work_name: str,
    target_pages: int,
    instruction: str,
    commit: bool = True,
) -> StoryGenerationSessionRecord:
    record = StoryGenerationSessionRecord(
        id=str(uuid.uuid4()),
        project_id=project_id,
        work_name=work_name,
        target_pages=target_pages,
        instruction=instruction,
        stages_json=json.dumps(empty_stages(), ensure_ascii=False),
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(record)
    if commit:
        session.commit()
        session.refresh(record)
    else:
        session.flush()
    return record


# --- 知識コンテキスト ---


@dataclass
class KnowledgeContext:
    required_text: str = ""
    reference_text: str = ""
    knowledge_ids: list[str] = field(default_factory=list)


def format_chunk(chunk: KnowledgeChunkRecord) -> str:
    header = f"[{chunk.kind or 'doc'}] {chunk.title}".strip()
    parts = [header, chunk.content]
    if chunk.policy:
        parts.append(f"(方針: {chunk.policy})")
    return "\n".join(part for part in parts if part).strip()


def build_context(
    session: Session,
    settings: Settings,
    work_name: str,
    query: str,
    reference_limit: int = 8,
) -> KnowledgeContext:
    if not work_name:
        return KnowledgeContext()
    budget = max(settings.llm_max_context_chars, 1000)
    required = knowledge.get_required_chunks(session, work_name)
    knowledge_ids: list[str] = []
    required_parts: list[str] = []
    used = 0
    required_budget = budget // 2
    for chunk in required:
        text = format_chunk(chunk)
        if used + len(text) > required_budget and required_parts:
            break
        required_parts.append(text)
        knowledge_ids.append(chunk.id)
        used += len(text)

    reference_parts: list[str] = []
    if query:
        hits = knowledge.search_chunks(
            session, work_name=work_name, query=query, usage="reference", limit=reference_limit
        )
        for chunk, _score, _method in hits:
            text = format_chunk(chunk)
            if used + len(text) > budget and reference_parts:
                break
            reference_parts.append(text)
            knowledge_ids.append(chunk.id)
            used += len(text)
    return KnowledgeContext(
        required_text="\n\n".join(required_parts),
        reference_text="\n\n".join(reference_parts),
        knowledge_ids=knowledge_ids,
    )


def stage_query(stage: str, record: StoryGenerationSessionRecord, stages: dict) -> str:
    pieces = [record.work_name, record.instruction]
    brief = stages["brief"].get("data") or {}
    plot = stages["plot"].get("data") or {}
    pages = stages["pages"].get("data") or {}
    if stage == "brief":
        pieces.append("あらすじ トーン キャラクター 設定 原作")
    elif stage == "plot":
        pieces.append(str(brief.get("synopsis", "")))
        pieces.append("プロット 起承転結 展開")
    elif stage == "pages":
        pieces.append(str(brief.get("synopsis", "")))
        pieces.extend(str(beat) for beat in plot.get("beats", []))
        pieces.append("場面 シーン 構成")
    elif stage == "script":
        for outline in pages.get("pages", []):
            pieces.append(str(outline.get("purpose", "")))
            pieces.append(str(outline.get("setting", "")))
        pieces.append("演出 コマ 台詞 効果音")
    return " ".join(piece for piece in pieces if piece)


# --- 段階生成 ---


def require_previous_ready(stage: str, stages: dict) -> None:
    """前段階が生成済み（データあり）であることだけを要求する。

    承認フローは廃止したため、前段階の生成が終われば次段階を生成できる。
    不満があれば各段階を再生成すればよい。
    """
    index = STAGE_ORDER.index(stage)
    if index == 0:
        return
    previous = STAGE_ORDER[index - 1]
    if stages[previous]["status"] == "empty" or stages[previous]["data"] is None:
        raise StoryError(f"前段階「{previous}」を生成してから生成してください")


def invalidate_downstream(stages: dict, stage: str) -> None:
    index = STAGE_ORDER.index(stage)
    for downstream in STAGE_ORDER[index + 1 :]:
        if stages[downstream]["status"] != "empty":
            stages[downstream]["data"] = None
            stages[downstream]["status"] = "empty"
            stages[downstream]["error"] = None
            stages[downstream]["warnings"] = []


def validate_stage_data(stage: str, data: dict, target_pages: int) -> dict:
    # LLMがpages/script段階でラッパーを省き配列だけを返すことがあるため吸収する。
    if stage in {"pages", "script"} and isinstance(data, list):
        data = {"pages": data}
    model = STAGE_MODELS[stage]
    validated = model.model_validate(data)
    if stage == "pages":
        assert isinstance(validated, PagesStage)
        if len(validated.pages) != target_pages:
            raise ValueError(
                f"ページ数は{target_pages}にしてください（現在{len(validated.pages)}）"
            )
    if stage == "script":
        assert isinstance(validated, ScriptStage)
        if len(validated.pages) != target_pages:
            raise ValueError(
                f"ページ数は{target_pages}にしてください（現在{len(validated.pages)}）"
            )
    return validated.model_dump()


logger = logging.getLogger(__name__)


# 段階生成のライブ進捗（session_id -> 進捗スナップショット）。
# ローカル単一プロセス前提の軽量な可観測性。生成中はLLM出力の受信状況を保持し、
# フロントエンドが別リクエストでポーリングして「止まっていないこと」を表示する。
_GENERATION_PROGRESS: dict[str, dict] = {}
_ACTIVE_GENERATION_SESSIONS: set[str] = set()


def get_generation_progress(session_id: str) -> dict | None:
    """進行中の段階生成の進捗スナップショットを返す（無ければNone）。"""
    return _GENERATION_PROGRESS.get(session_id)


def _start_generation_progress(session_id: str, stage: str) -> None:
    if session_id in _ACTIVE_GENERATION_SESSIONS:
        raise StoryError("このストーリーセッションは生成中です", status_code=409)
    _ACTIVE_GENERATION_SESSIONS.add(session_id)
    timestamp = now_utc().isoformat()
    _GENERATION_PROGRESS[session_id] = {
        "stage": stage,
        "phase": "running",
        "chars": 0,
        "tail": "",
        "started_at": timestamp,
        "updated_at": timestamp,
    }


def _clear_generation_progress(session_id: str) -> None:
    _GENERATION_PROGRESS.pop(session_id, None)
    _ACTIVE_GENERATION_SESSIONS.discard(session_id)


async def free_comfyui_vram(settings: Settings) -> None:
    """ComfyUIの/freeでVRAMを解放する（プロセスは落とさない）。ベストエフォート。

    台本(LLM)生成の直前に呼び、ComfyUIの画像モデルをVRAMから退避させて、LLMが
    VRAMを確保できるようにする。LLM側はOllama OLLAMA_KEEP_ALIVE=0等で各生成後に
    自動退避する想定。失敗しても生成は止めない。
    """
    base = settings.comfyui_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base}/free", json={"unload_models": True, "free_memory": True}
            )
            # 404/405/500等はhttpxでは例外にならないため明示的に検査する（可観測性）。
            response.raise_for_status()
    except Exception as exc:  # 接続不可・HTTPエラー・未対応でも生成は継続する
        logger.debug("ComfyUI /free に失敗しました（VRAM解放はスキップ）: %s", exc)


async def generate_stage(
    session: Session,
    llm,
    settings: Settings,
    record: StoryGenerationSessionRecord,
    stage: str,
    instruction: str = "",
) -> StoryGenerationSessionRecord:
    if stage not in STAGE_ORDER:
        raise StoryError("不正な段階です", status_code=422)
    stages = load_stages(record)
    require_previous_ready(stage, stages)
    context = build_context(session, settings, record.work_name, stage_query(stage, record, stages))

    session_id = record.id

    def on_progress(text: str) -> None:
        # LLMから受信した累積出力の文字数と末尾を進捗へ反映する（止まっていない証跡）。
        snapshot = _GENERATION_PROGRESS.get(session_id)
        if snapshot is None:
            return
        snapshot["chars"] = len(text)
        snapshot["tail"] = text[-120:]
        snapshot["updated_at"] = now_utc().isoformat()

    _start_generation_progress(session_id, stage)
    error: str | None = None
    review_warnings: list[str] = []
    try:
        try:
            if isinstance(llm, StubLLMClient):
                data = generate_stub_stage(session, record, stages, stage)
                data = validate_stage_data(stage, data, record.target_pages)
            else:
                # 実LLM呼び出し前にComfyUIのVRAMを解放してVRAM同居を避ける（任意・既定OFF）。
                if settings.comfyui_free_before_llm:
                    await free_comfyui_vram(settings)
                data = await generate_llm_stage(
                    llm, record, stages, stage, context, instruction, on_progress=on_progress
                )
        except StoryError:
            raise
        except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            error = str(exc)
            data = None

        # 台本段階は生成後に1回だけ編集チェックを掛け、自動修正と警告を残す（領域2）。
        if stage == "script" and data is not None:
            original_data = copy.deepcopy(data)
            data, review_warnings = review_script(data)
            try:
                data = validate_stage_data(stage, data, record.target_pages)
            except (ValidationError, ValueError) as exc:
                # 編集チェックで壊れることは想定しないが、壊れたら修正前データへ戻す。
                error = error or str(exc)
                data = original_data
                review_warnings = []

        stages[stage]["data"] = data
        stages[stage]["knowledge_ids"] = context.knowledge_ids
        stages[stage]["error"] = error
        stages[stage]["warnings"] = review_warnings
        stages[stage]["status"] = "draft" if data is not None else "empty"
        stages[stage]["updated_at"] = now_utc().isoformat()
        invalidate_downstream(stages, stage)
        save_stages(session, record, stages)
        return record
    finally:
        _clear_generation_progress(session_id)


def update_stage(
    session: Session,
    record: StoryGenerationSessionRecord,
    stage: str,
    data: dict,
) -> StoryGenerationSessionRecord:
    if stage not in STAGE_ORDER:
        raise StoryError("不正な段階です", status_code=422)
    try:
        validated = validate_stage_data(stage, data, record.target_pages)
    except (ValidationError, ValueError) as exc:
        raise StoryError(f"段階データが不正です: {exc}", status_code=422) from exc
    stages = load_stages(record)
    stages[stage]["data"] = validated
    stages[stage]["status"] = "draft"
    stages[stage]["error"] = None
    stages[stage]["updated_at"] = now_utc().isoformat()
    invalidate_downstream(stages, stage)
    save_stages(session, record, stages)
    return record


def approve_stage(
    session: Session,
    record: StoryGenerationSessionRecord,
    stage: str,
) -> StoryGenerationSessionRecord:
    if stage not in STAGE_ORDER:
        raise StoryError("不正な段階です", status_code=422)
    stages = load_stages(record)
    require_previous_ready(stage, stages)
    if stages[stage]["data"] is None:
        raise StoryError("生成または編集してから承認してください")
    stages[stage]["status"] = "approved"
    stages[stage]["error"] = None
    save_stages(session, record, stages)
    return record


# --- LLM経由の生成 ---


def stage_instruction_text(stage: str) -> str:
    return {
        "brief": (
            "企画段階です。あらすじ(synopsis)、トーン(tone)、登場人物の役割(characters: [{name, role}])、"
            "原作準拠条件(canon_conditions: [string])をJSONで出力してください。"
        ),
        "plot": (
            "全体プロット段階です。起承転結(ki, sho, ten, ketsu)、主要ビート(beats: [string])、"
            "キャラアーク(character_arcs: [{name, arc}])をJSONで出力してください。"
        ),
        "pages": (
            "ページ構成段階です。指定ページ数ぶんのpages配列を出力してください。"
            "各要素は page(整数), purpose, setting, characters([string]), hook を持ちます。"
        ),
        "script": (
            "コマ台本段階です。各ページをプロの漫画のように読みやすいコマ割りへ分割してください。"
            "コマ割りの基準:"
            "・標準的な進行のページは3〜5コマに割る。テンポの速い場面や情報量の多いページは最大9コマまで増やしてよい（このシステムは1ページ最大9コマ）。"
            "・1ページを1コマにするのは、山場・見せ場・余韻など明確な演出意図があるページだけに限る。漫然と全ページを1コマにしない。"
            "・各コマで画角(shot)と役割を変え、状況提示→反応→引き のように流れを作る。"
            "・ページのpurposeとhookに沿ってコマを配置する。"
            "出力形式: pages配列。各ページは page(整数), page_goal, emotional_curve([string]), "
            "panels(1〜9個)を持ちます。"
            "各コマは shot, camera, role, emotion, background_density, location, visual_prompt, "
            "composition_notes, text_safe_area, characters([string]), dialogue, sfx を持ちます。"
            "roleは establish/dialogue/reaction/action/reveal/emotional_peak/silent/transition/"
            "punchline/aftermath から選びます。"
            "emotionはそのコマで読者に伝える感情ビートを短く書きます。"
            "background_densityは none/white/light/full から選び、白背景にする場合は演出意図を持たせてください。"
            "charactersにはそのコマに実際に描かれる登場人物名だけを列挙してください（画面外の話者は含めない）。"
            "台詞が無いコマでも、描かれる人物がいれば必ず列挙してください。"
            "可能なら character_directives: [{name, position, expression, action}] も出力し、"
            "人物ごとの画面内位置(position: upper_left/upper_right/lower_left/lower_right/center)、"
            "表情(expression)、動作(action)、region_box([x,y,w,h])を英語/数値で指定してください"
            "（画像生成promptと領域分離へ反映されます）。"
            "dialogueは [{speaker, text, kind, on_screen}] の配列です。"
            "kindは speech(会話)/monologue(独白・心の声)/narration(ナレーション・地の文)/shout(叫び) から選びます。"
            "on_screenは話者がそのコマに描かれていればtrue、画面外の声ならfalseにします。独白・ナレーションは原則false。"
            "擬音は台詞に混ぜず、必ずsfxへ入れてください。sfxは [{text, style}] の配列です（styleは handwritten/impact/quiet）。"
            "shotはコマ番号ではなく、close-up, medium shot, wide shotなどの画角を文字列で指定してください。"
            "camera, location, visual_prompt, speaker, textも文字列で出力してください。"
            "任意で各ページにlayout(コマ割りのヒント文字列)を付けてもかまいません。"
            "visual_promptは英語の画像生成プロンプトにし、bboxやコマ枠座標は出力しないでください。"
        ),
    }[stage]


def build_stage_messages(
    record: StoryGenerationSessionRecord,
    stages: dict,
    stage: str,
    context: KnowledgeContext,
    instruction: str,
    retry_error: str | None = None,
    previous_output: str | None = None,
    directive: str | None = None,
) -> list[dict]:
    system = (
        "あなたは同人誌のネーム制作を支援するアシスタントです。"
        "必ず指示されたスキーマのJSONオブジェクトのみを出力し、余計な説明やコードフェンスを付けないでください。"
    )
    upstream: list[str] = []
    for previous in STAGE_ORDER[: STAGE_ORDER.index(stage)]:
        data = stages[previous].get("data")
        if data is not None:
            upstream.append(f"## {previous}\n{json.dumps(data, ensure_ascii=False)}")

    parts = [
        f"作品名: {record.work_name or '未設定'}",
        f"ページ数: {record.target_pages}",
        f"全体方針: {record.instruction or 'なし'}",
        stage_instruction_text(stage),
    ]
    if instruction:
        parts.append(f"今回の追加指示: {instruction}")
    if context.required_text:
        parts.append("# 必須条件（必ず守る原作準拠情報）\n" + context.required_text)
    if context.reference_text:
        parts.append("# 参考情報（任意で活用する）\n" + context.reference_text)
    if upstream:
        parts.append("# 承認済みの前段階\n" + "\n\n".join(upstream))
    if directive:
        parts.append(
            "前回の出力には次の問題がありました。修正して再出力してください:\n" + directive
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    if retry_error:
        if previous_output:
            messages.append({"role": "assistant", "content": previous_output})
        messages.append(
            {
                "role": "user",
                "content": (
                    "上記の出力は検証に失敗しました。内容を維持したまま次のエラーを修正し、"
                    "完全で有効なJSONオブジェクトのみを再出力してください:\n" + retry_error
                ),
            }
        )
    return messages


PANELING_DIRECTIVE = (
    "全ページが1コマになっています。山場などの演出意図があるページ以外は、"
    "1ページを複数コマに分割し直してください。標準的な進行のページは3〜5コマを目安にします。"
)


def script_needs_repaneling(data: dict) -> bool:
    """台本が「全ページ1コマ」の退化状態かどうか（2ページ以上が前提）。"""
    pages = data.get("pages", []) if isinstance(data, dict) else []
    if len(pages) < 2:
        return False
    return all(len(page.get("panels", [])) <= 1 for page in pages)


# --- 台本の編集チェック（領域2） ---

# 純カタカナ（＋長音・末尾の感嘆符）は台詞ではなく擬音とみなす。
_ONOMATOPOEIA_RE = re.compile(r"^[ァ-ヶー]{1,8}[！!]*$")
# 擬音欄に紛れがちな「音ではない語」。場面と対応しないため除去する（例: トドメ）。
_SFX_WORD_BLOCKLIST = {
    "トドメ",
    "とどめ",
    "止め",
    "攻撃",
    "勝利",
    "敗北",
    "成功",
    "失敗",
    "完了",
    "終了",
}


def _looks_like_shout(text: str) -> bool:
    stripped = text.strip()
    # 末尾に「！」が2つ以上で、語そのものがある（擬音ではない）場合は叫び。
    return bool(re.search(r"[！!]{2,}$", stripped)) and not _ONOMATOPOEIA_RE.match(stripped)


def _looks_like_monologue(text: str) -> bool:
    stripped = text.strip()
    return bool(re.fullmatch(r"[（(].+[）)]", stripped))


def review_script(data: dict) -> tuple[dict, list[str]]:
    """LLM/スタブ生成後の台本へ1回だけ編集チェックを掛ける（領域2）。

    台詞と擬音の混同、独白/叫びの種別誤り、場面非対応の擬音などを自動修正し、
    行った修正を警告メッセージとして返す。破壊的変更はせず、内容は保持する。
    """
    warnings: list[str] = []
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        return data, warnings
    for page in pages:
        page_no = page.get("page", "?")
        panels = page.get("panels", []) if isinstance(page, dict) else []
        page_has_character = False
        for p_index, panel in enumerate(panels, start=1):
            if not isinstance(panel, dict):
                continue
            label = f"{page_no}ページ コマ{p_index}"
            # character_directivesも人物指定とみなす（directive単独ページの誤警告を防ぐ）。
            if panel.get("characters") or panel.get("character_directives"):
                page_has_character = True
            dialogue = panel.get("dialogue", []) or []
            sfx = panel.get("sfx", []) or []
            kept_dialogue: list[dict] = []
            for line in dialogue:
                if not isinstance(line, dict):
                    continue
                text = str(line.get("text", "")).strip()
                if not text:
                    continue
                # 1) 擬音らしき台詞は内容を破壊しないよう自動移動せず、警告だけ出す
                #    （「ダメ！」「ハイ！」等の自然な会話の誤変換を避ける）。
                if _ONOMATOPOEIA_RE.match(text):
                    warnings.append(
                        f"{label}: 台詞「{text}」は擬音の可能性があります（擬音欄への移動を検討）"
                    )
                # 2) 独白を通常会話で書いている → kindをmonologueへ。
                if _looks_like_monologue(text) and line.get("kind", "speech") == "speech":
                    line["kind"] = "monologue"
                    line["on_screen"] = False
                    warnings.append(f"{label}: 括弧書きの台詞を独白に変更しました")
                # 3) 叫びがspeechのまま → kindをshoutへ。
                elif _looks_like_shout(text) and line.get("kind", "speech") == "speech":
                    line["kind"] = "shout"
                    warnings.append(f"{label}: 強い語気の台詞を叫びに変更しました")
                kept_dialogue.append(line)
            # 4) 場面非対応の擬音は、単語一致だけで内容を判断できないため削除せず警告に留める。
            cleaned_sfx: list[dict] = []
            for item in sfx:
                sfx_text = (
                    str(item.get("text", "")).strip()
                    if isinstance(item, dict)
                    else str(item).strip()
                )
                if not sfx_text:
                    continue
                if sfx_text in _SFX_WORD_BLOCKLIST:
                    warnings.append(
                        f"{label}: 擬音「{sfx_text}」は場面と対応しない可能性があります（要確認）"
                    )
                cleaned_sfx.append(item if isinstance(item, dict) else {"text": sfx_text})
            panel["dialogue"] = kept_dialogue
            panel["sfx"] = cleaned_sfx
        # 5) 必須人物チェック: 人物も台詞も無いページは登場人物欠落として警告。
        if panels and not page_has_character:
            has_dialogue = any(panel.get("dialogue") for panel in panels if isinstance(panel, dict))
            if not has_dialogue:
                warnings.append(f"{page_no}ページ: 登場人物が指定されていません")
    return data, warnings


async def generate_llm_stage(
    llm,
    record: StoryGenerationSessionRecord,
    stages: dict,
    stage: str,
    context: KnowledgeContext,
    instruction: str,
    on_progress=None,
) -> dict:
    async def attempt(directive: str | None) -> dict:
        messages = build_stage_messages(
            record, stages, stage, context, instruction, directive=directive
        )
        last_error: Exception | None = None
        max_attempts = 3 if stage == "script" else 2
        for retry in range(max_attempts):
            content = await llm.chat(messages, want_json=True, on_progress=on_progress)
            try:
                parsed = extract_json_object(content)
                return validate_stage_data(stage, parsed, record.target_pages)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if retry < max_attempts - 1:
                    # 長いコマ台本は2回、それ以外は1回だけ元出力付きで修正要求する。
                    messages = build_stage_messages(
                        record,
                        stages,
                        stage,
                        context,
                        instruction,
                        retry_error=str(exc),
                        previous_output=content,
                        directive=directive,
                    )
                else:
                    raise
        raise last_error if last_error else RuntimeError("LLM生成に失敗しました")

    result = await attempt(None)
    # 台本が全ページ1コマの退化状態なら、一度だけコマ割りの作り直しを促す。
    if stage == "script" and script_needs_repaneling(result):
        try:
            result = await attempt(PANELING_DIRECTIVE)
        except (LLMError, ValidationError, ValueError, json.JSONDecodeError):
            pass  # 作り直しに失敗したら元の結果を採用する。
    return result


# --- スタブ生成 ---


def stub_character_names(session: Session, work_name: str) -> list[str]:
    names: list[str] = []
    if work_name:
        chunks = (
            session.query(KnowledgeChunkRecord)
            .filter(
                KnowledgeChunkRecord.work_name == work_name,
                KnowledgeChunkRecord.kind == "character",
            )
            .all()
        )
        for chunk in chunks:
            if chunk.title and chunk.title not in names:
                names.append(chunk.title)
    return names


def generate_stub_stage(
    session: Session,
    record: StoryGenerationSessionRecord,
    stages: dict,
    stage: str,
) -> dict:
    work_name = record.work_name or "本作"
    target = record.target_pages
    if stage == "brief":
        names = stub_character_names(session, record.work_name) or ["主役", "相方"]
        required = knowledge.get_required_chunks(session, record.work_name)
        canon = [f"{chunk.title or chunk.kind or '設定'}を守る" for chunk in required] or [
            "原作の雰囲気を保つ"
        ]
        brief = BriefStage(
            synopsis=(record.instruction or f"{work_name}を舞台にした{target}ページの短編。"),
            tone="原作準拠で軽妙",
            characters=[
                BriefCharacter(name=name, role="主役" if index == 0 else "登場人物")
                for index, name in enumerate(names[:4])
            ],
            canon_conditions=canon,
        )
        return brief.model_dump()
    if stage == "plot":
        brief = stages["brief"].get("data") or {}
        synopsis = brief.get("synopsis", f"{work_name}の物語")
        characters = brief.get("characters", []) or [{"name": "主役", "role": "主役"}]
        plot = PlotStage(
            ki=f"導入: {synopsis}",
            sho="展開: 二人の関係や状況が動き出す。",
            ten="転換: 予想外の出来事で空気が変わる。",
            ketsu="結末: 指定の方向で余韻を残して締める。",
            beats=[f"ビート{index + 1}" for index in range(max(target // 2, 2))],
            character_arcs=[
                CharacterArc(name=c.get("name", "主役"), arc="心情が一歩動く") for c in characters
            ],
        )
        return plot.model_dump()
    if stage == "pages":
        brief = stages["brief"].get("data") or {}
        plot = stages["plot"].get("data") or {}
        names = [c.get("name", "主役") for c in brief.get("characters", [])] or ["主役", "相方"]
        beats = plot.get("beats", []) or ["導入", "展開", "転換", "結末"]
        outlines = []
        for page_number in range(1, target + 1):
            beat = beats[(page_number - 1) % len(beats)]
            outlines.append(
                PageOutline(
                    page=page_number,
                    purpose=f"{beat}を描く",
                    setting="基本の舞台",
                    characters=names[: 2 if page_number % 2 else 1],
                    hook=f"{page_number + 1}ページへ引く"
                    if page_number < target
                    else "オチで締める",
                )
            )
        return PagesStage(pages=outlines).model_dump()
    if stage == "script":
        pages_stage = stages["pages"].get("data") or {}
        outlines = pages_stage.get("pages", [])
        script_pages = []
        for outline in outlines:
            page_number = int(outline.get("page", 1))
            characters = outline.get("characters", []) or ["主役"]
            panel_count = 4 if page_number % 2 == 0 else 3
            panels = []
            for panel_index in range(panel_count):
                speaker = characters[panel_index % len(characters)]
                panels.append(
                    {
                        "shot": "バストアップ" if panel_index % 2 else "ロングショット",
                        "camera": "正面" if panel_index % 2 else "やや俯瞰",
                        "location": outline.get("setting", "基本の舞台"),
                        "visual_prompt": (
                            f"anime style, {outline.get('purpose', 'scene')}, "
                            f"{'close up expressive face' if panel_index % 2 else 'establishing shot'}"
                        ),
                        "characters": [speaker]
                        if panel_index == panel_count - 1
                        else list(characters),
                        "dialogue": [
                            {"speaker": speaker, "text": f"{outline.get('purpose', '場面')}…"}
                        ]
                        if panel_index != panel_count - 1
                        else [],
                        "sfx": ["しーん"]
                        if panel_index == panel_count - 1 and page_number % 2
                        else [],
                    }
                )
            script_pages.append({"page": page_number, "panels": panels})
        return ScriptStage.model_validate({"pages": script_pages}).model_dump()
    raise StoryError("不正な段階です", status_code=422)


# --- Manga JSON変換と適用 ---


def distribute_rows(panel_count: int, max_cols: int = 3) -> list[int]:
    """コマ数を1行あたり最大max_colsで均等な行構成へ分配する。"""
    panel_count = max(1, panel_count)
    n_rows = math.ceil(panel_count / max_cols)
    base, extra = divmod(panel_count, n_rows)
    # 余りは上の行から1コマずつ足す（読み始めを情報量多めにする）。
    return [base + (1 if i < extra else 0) for i in range(n_rows)]


def grid_layout(
    panel_count: int, margin: float = 0.04, gap: float = 0.012
) -> list[tuple[float, float, float, float]]:
    """任意コマ数を行グリッドのbboxへ自動配置する（1〜4以外のフォールバック）。"""
    rows = distribute_rows(panel_count)
    usable_h = 1 - 2 * margin - gap * (len(rows) - 1)
    row_h = usable_h / len(rows)
    boxes: list[tuple[float, float, float, float]] = []
    y = margin
    for cols in rows:
        usable_w = 1 - 2 * margin - gap * (cols - 1)
        col_w = usable_w / cols
        x = margin
        for _ in range(cols):
            boxes.append((round(x, 4), round(y, 4), round(col_w, 4), round(row_h, 4)))
            x += col_w + gap
        y += row_h + gap
    return boxes


def select_layout(panel_count: int, hint: str) -> list[tuple[float, float, float, float]]:
    options = PANEL_LAYOUTS.get(panel_count)
    if options:
        if hint and hint in options:
            return options[hint]
        return options["default"]
    # 5コマ以上は動的グリッドで配置する。
    return grid_layout(panel_count)


def match_character_id(name: str, characters: list[Character]) -> str | None:
    """話者名を表示名・別名・部分一致でキャラIDへ解決する。"""
    name = (name or "").strip()
    if not name:
        return None
    for character in characters:
        if name == character.id or name == character.display_name or name in character.aliases:
            return character.id
    # 「城ヶ崎美嘉さん」「美嘉」などの表記揺れを部分一致で吸収する。
    for character in characters:
        for candidate in (character.display_name, *character.aliases):
            if candidate and (candidate in name or name in candidate):
                return character.id
    return None


def resolve_character_ids(names: list[str], manga: MangaProject) -> list[str]:
    resolved: list[str] = []
    for name in names:
        character_id = match_character_id(name, manga.characters)
        if character_id and character_id not in resolved:
            resolved.append(character_id)
    return resolved


def _has_ascii_letters(text: str) -> bool:
    """英字（booruタグの最低条件）を含むか。"""
    return any("a" <= ch.lower() <= "z" for ch in text)


def _is_weak_trigger(character: Character) -> bool:
    """画像生成トークンとして弱いtriggerかどうか。

    未設定・表示名そのまま・英字を含まない（日本語名のみ）triggerは、素モデルが
    解釈できず人物が描き分けられないため弱trigger扱いし、知識DBのboooruタグで上書きする。
    例: 「城ヶ崎莉嘉」→ 弱 → 「jougasaki rika, idolmaster cinderella girls」で補完。
    """
    trigger = (character.trigger_prompt or "").strip()
    if not trigger or trigger == character.display_name.strip():
        return True
    return not _has_ascii_letters(trigger)


def _merge_duplicate_character(existing: Character, candidate: Character) -> Character:
    """同名キャラの重複を、ID維持と情報保持を優先して統合する。"""
    merged = existing.model_copy(deep=True)
    if _is_weak_trigger(merged) and not _is_weak_trigger(candidate):
        merged.trigger_prompt = candidate.trigger_prompt

    aliases = list(merged.aliases)
    for alias in candidate.aliases:
        if alias not in aliases:
            aliases.append(alias)
    merged.aliases = aliases

    if not merged.appearance_prompt:
        merged.appearance_prompt = candidate.appearance_prompt
    if not merged.outfit_prompt:
        merged.outfit_prompt = candidate.outfit_prompt
    if not merged.negative_prompt:
        merged.negative_prompt = candidate.negative_prompt
    if candidate.lora_name and not merged.lora_name:
        merged.lora_node_id = candidate.lora_node_id
        merged.lora_name = candidate.lora_name
    if not merged.speech_style:
        merged.speech_style = candidate.speech_style
    return merged


def _get_character_chunks_for_profile_merge(
    session: Session, work_name: str
) -> list[KnowledgeChunkRecord]:
    """同名chunkの情報統合専用に、重複を残したcharacter chunkを取得する。"""
    if not work_name:
        return []
    return (
        session.query(KnowledgeChunkRecord)
        .filter(
            KnowledgeChunkRecord.work_name == work_name,
            KnowledgeChunkRecord.kind == "character",
        )
        .order_by(
            KnowledgeChunkRecord.usage,
            KnowledgeChunkRecord.source_id,
            KnowledgeChunkRecord.position,
        )
        .all()
    )


def build_characters_from_knowledge(session: Session, work_name: str) -> list[Character]:
    """知識DBのキャラクター種別チャンクからCharacterプロファイルを生成する。

    同じ人物のチャンクが重複している場合（旧データの残骸など）は、英字triggerを持つ
    強い方を優先して1人に統合する。これにより、triggerが空の重複チャンクが正しい
    booruタグを上書きして人物が描けなくなる事故を防ぐ。
    """
    by_display: dict[str, Character] = {}
    order: list[str] = []
    used_ids: set[str] = set()
    for index, chunk in enumerate(_get_character_chunks_for_profile_merge(session, work_name)):
        display_name = (chunk.title or "").strip()
        if not display_name:
            continue
        image = knowledge.parse_chunk_image(chunk)
        char_id = str(image.get("id") or f"kc_{index + 1}").strip() or f"kc_{index + 1}"
        aliases = [str(alias).strip() for alias in image.get("aliases", []) if str(alias).strip()]
        candidate = Character(
            id=char_id,
            display_name=display_name,
            aliases=aliases,
            trigger_prompt=str(image.get("trigger_prompt", "")).strip() or display_name,
            appearance_prompt=str(image.get("appearance_prompt", "")).strip(),
            outfit_prompt=str(image.get("outfit_prompt", "")).strip(),
            negative_prompt=str(image.get("negative_prompt", "")).strip(),
            lora_node_id=str(image.get("lora_node_id", "")).strip(),
            lora_name=str(image.get("lora_name", "")).strip(),
            speech_style=str(image.get("speech_style", "")).strip(),
        )
        existing = by_display.get(display_name)
        if existing is None:
            order.append(display_name)
            by_display[display_name] = candidate
            continue
        # 重複時は既存IDと詳細情報を維持し、強いtriggerなど不足分だけ補完する。
        by_display[display_name] = _merge_duplicate_character(existing, candidate)

    characters: list[Character] = []
    for display_name in order:
        character = by_display[display_name]
        while character.id in used_ids:
            character.id = f"{character.id}_x"
        used_ids.add(character.id)
        characters.append(character)
    return characters


def merge_knowledge_characters(base: MangaProject, knowledge_characters: list[Character]) -> None:
    """baseにキャラを取り込む。新規は追加し、既存はtriggerが弱ければ画像情報を補完する。"""
    for known in knowledge_characters:
        match = next(
            (c for c in base.characters if match_character_id(known.display_name, [c]) is not None),
            None,
        )
        if match is None:
            base.characters.append(known)
            continue
        # ユーザーが調整済み(=強いtrigger)なら尊重し、未設定のみ補完する。
        if _is_weak_trigger(match):
            match.trigger_prompt = known.trigger_prompt
            match.negative_prompt = match.negative_prompt or known.negative_prompt
        if not match.appearance_prompt:
            match.appearance_prompt = known.appearance_prompt
        if not match.outfit_prompt:
            match.outfit_prompt = known.outfit_prompt
        if not match.aliases:
            match.aliases = known.aliases
        if known.lora_name and not match.lora_name:
            match.lora_node_id = known.lora_node_id
            match.lora_name = known.lora_name
        if not match.speech_style:
            match.speech_style = known.speech_style


def resolve_location_id(location: str, manga: MangaProject) -> str:
    for item in manga.locations:
        if location and (location == item.id or location == item.display_name):
            return item.id
    return manga.locations[0].id if manga.locations else ""


# subject_mode自動分類用のキーワード（shot/visual_prompt/cameraを小文字化して照合）。
_HAND_KEYWORDS = ["手元", "手のひら", "手だけ", "指先", "hand", "hands", "fingers", "palm"]
_PROP_KEYWORDS = [
    "小物",
    "小箱",
    "ボタン",
    "アイテム",
    "持ち物",
    "商品",
    "物だけ",
    "prop",
    "object",
    "product",
    "item",
    "still life",
    "close-up of a",
]
_BACKGROUND_KEYWORDS = [
    "背景",
    "風景",
    "情景",
    "遠景",
    "空",
    "街",
    "建物",
    "background",
    "scenery",
    "landscape",
    "establishing",
    "empty room",
    "wide shot of",
]


def classify_subject_mode(script_panel: ScriptPanel) -> str:
    """コマ台本から主題モードを推定する（領域1）。

    手元・小物・背景コマを検出し、人物LoRA/参照を注入しない非人物モードへ振り分ける。
    台本が人物を明示したコマ（charactersは「実際に描かれる人物だけ」の契約。
    character_directivesも人物指定とみなす）と、画面内の会話・叫び話者がいるコマは
    人物コマとして最優先で尊重する。画面外ナレーションだけの背景コマは非人物のまま
    扱い、ページ人物の再混入を防ぐ。
    """
    if script_panel.characters or script_panel.character_directives:
        return "character_scene"
    # 話者名のある画面内の会話・叫びだけを人物コマの根拠にする。speaker省略（既定の
    # speech/on_screen）の台詞本文だけでは人物コマと断定しない（背景コマの誤分類を防ぐ）。
    has_visible_speaker = any(
        line.speaker.strip() and line.on_screen and line.kind in {"speech", "shout"}
        for line in script_panel.dialogue
    )
    if has_visible_speaker:
        return "character_scene"
    haystack = " ".join(
        [script_panel.shot, script_panel.camera, script_panel.visual_prompt]
    ).casefold()
    if any(keyword.casefold() in haystack for keyword in _HAND_KEYWORDS):
        return "hand_insert"
    if any(keyword.casefold() in haystack for keyword in _BACKGROUND_KEYWORDS):
        return "background"
    if any(keyword.casefold() in haystack for keyword in _PROP_KEYWORDS):
        return "prop_insert"
    return "character_scene"


# 複数擬音が中央へ集中しないよう分散させる既定アンカー。
_SFX_SPREAD_ANCHORS = ["upper_right", "lower_left", "upper_left", "lower_right", "center"]


def build_panel_sfx(script_panel: ScriptPanel) -> list[Sfx]:
    """台本の擬音をSfxへ変換する。位置未指定の複数擬音は重ならないよう分散する。"""
    items = [item for item in script_panel.sfx if item.text.strip()]
    all_centered = all(item.position == "center" for item in items)
    sfx_list: list[Sfx] = []
    for index, item in enumerate(items):
        if all_centered and len(items) > 1:
            position = _SFX_SPREAD_ANCHORS[index % len(_SFX_SPREAD_ANCHORS)]
            # 上段と下段でサイズを変え、視覚的にも分離する。
            font_size = 64 if index % 2 == 0 else 48
        else:
            position = item.position
            font_size = 54
        sfx_list.append(
            Sfx(
                text=item.text[:40],
                position=position,
                style=item.style or "small_handwritten",
                font_size=font_size,
            )
        )
    return sfx_list


# 人物解決の警告に付ける安定prefix。再適用時にこの種別だけ再計算・置換する。
UNRESOLVED_CHARACTER_PREFIX = "[未解決の人物] "

POSITION_REGION_BOXES: dict[str, tuple[float, float, float, float]] = {
    "upper_left": (0.02, 0.02, 0.46, 0.46),
    "upper_right": (0.52, 0.02, 0.46, 0.46),
    "lower_left": (0.02, 0.52, 0.46, 0.46),
    "lower_right": (0.52, 0.52, 0.46, 0.46),
    "center": (0.18, 0.08, 0.64, 0.84),
}


SCRIPT_ROLE_ALIASES = {
    "establishing": "establish",
    "setup": "establish",
    "conversation": "dialogue",
    "closeup": "reaction",
    "close_up": "reaction",
    "insert": "transition",
    "climax": "emotional_peak",
    "quiet": "silent",
    "silence": "silent",
}


def normalize_script_role(value: str) -> str:
    """台本のrole表記を検査しやすい語彙へ寄せる。"""
    normalized = (value or "").strip().casefold().replace(" ", "_").replace("-", "_")
    normalized = SCRIPT_ROLE_ALIASES.get(normalized, normalized)
    valid = {
        "establish",
        "dialogue",
        "reaction",
        "action",
        "reveal",
        "emotional_peak",
        "silent",
        "transition",
        "punchline",
        "aftermath",
        "montage",
    }
    return normalized if normalized in valid else ""


def default_region_box(position: str, index: int, total: int) -> tuple[float, float, float, float]:
    """人物領域が未指定のとき、位置指定と人数から決定的な初期領域を作る。"""
    if position in POSITION_REGION_BOXES:
        return POSITION_REGION_BOXES[position]
    if total <= 1:
        return POSITION_REGION_BOXES["center"]
    width = 1.0 / total
    return (round(index * width, 4), 0.05, round(width, 4), 0.9)


def merge_unresolved_warnings(existing: list[str], unresolved: list[str]) -> list[str]:
    """既存warningsのうち未解決人物カテゴリだけを置換する。

    安定prefixの警告を毎回作り直すことで、誤字を直して再適用すれば古い警告が消え、
    他カテゴリ（編集チェック等）の警告は保持される。
    """
    kept = [warning for warning in existing if not warning.startswith(UNRESOLVED_CHARACTER_PREFIX)]
    return kept + unresolved


def script_to_manga(
    script: ScriptStage,
    base: MangaProject,
    page_characters: dict[int, list[str]] | None = None,
    warnings: list[str] | None = None,
) -> MangaProject:
    common_positive = base.common_positive_prompt or DEFAULT_COMMON_POSITIVE_PROMPT
    common_negative = base.common_negative_prompt or DEFAULT_COMMON_NEGATIVE_PROMPT
    page_characters = page_characters or {}

    rtl = base.reading_direction == "rtl"
    total_pages = len(script.pages)
    previous_family: str | None = None
    pages: list[Page] = []
    for page_index, script_page in enumerate(script.pages):
        panel_count = len(script_page.panels)
        family = layout_engine.choose_family(
            script_page.layout, page_index, total_pages, panel_count, previous_family
        )
        previous_family = family
        layout = layout_engine.build_page_layout(panel_count, family, base.page_layout, rtl=rtl)
        panels: list[Panel] = []
        for index, script_panel in enumerate(script_page.panels):
            panel_id = f"p{script_page.page:02d}_{index + 1:02d}"
            role = normalize_script_role(script_panel.role) or layout_engine.derive_role(
                index, panel_count, page_index, total_pages, bool(script_panel.dialogue)
            )
            subject_mode = classify_subject_mode(script_panel)
            non_character = subject_mode in {"prop_insert", "hand_insert", "background"}
            # 明示された人物名（characters/directive/画面内話者）が登録キャラに解決できない
            # 場合は、黙って人物なしコマにせず警告として残す（表記揺れ・誤字の検知）。
            if warnings is not None:
                explicit_names = [
                    *script_panel.characters,
                    *(directive.name for directive in script_panel.character_directives),
                    *(
                        line.speaker
                        for line in script_panel.dialogue
                        if line.speaker and line.on_screen and line.kind in {"speech", "shout"}
                    ),
                ]
                for name in explicit_names:
                    if name and match_character_id(name, base.characters) is None:
                        message = f"{UNRESOLVED_CHARACTER_PREFIX}{panel_id}: 「{name}」を登録キャラクターに解決できません"
                        if message not in warnings:
                            warnings.append(message)
            # コマ明記の登場人物 + 人物別ディレクションの名前に、画面内の会話/叫び話者だけを
            # 足す（画面外台詞は描画人物に含めない）。directiveだけ指定された人物も取りこぼさない。
            names: list[str] = list(script_panel.characters)
            for directive in script_panel.character_directives:
                if directive.name and directive.name not in names:
                    names.append(directive.name)
            for line in script_panel.dialogue:
                if (
                    line.speaker
                    and line.on_screen
                    and line.kind in {"speech", "shout"}
                    and line.speaker not in names
                ):
                    names.append(line.speaker)
            # 人物指定の意図（characters/directive/画面内話者）が一つでもあれば、
            # 解決できなくてもページ人物へfallbackしない（未知名経由の再混入を防ぐ）。
            has_explicit_character_intent = bool(names)
            character_ids = resolve_character_ids(names, base)
            if not character_ids and not non_character and not has_explicit_character_intent:
                character_ids = resolve_character_ids(
                    page_characters.get(script_page.page, []), base
                )
            # 手元・小物・背景コマは人物指定を外す（LoRA混入を防ぐ）。
            if non_character:
                character_ids = []
            # 人物別ディレクション（位置・表情・動作）をIDで引けるようにする。
            directive_by_id: dict[str, ScriptCharacter] = {}
            for directive in script_panel.character_directives:
                directive_id = match_character_id(directive.name, base.characters)
                if directive_id and directive_id not in directive_by_id:
                    directive_by_id[directive_id] = directive
            character_layout = []
            for i, character_id in enumerate(character_ids):
                directive = directive_by_id.get(character_id)
                if directive is not None:
                    position = directive.position
                else:
                    position = _SFX_SPREAD_ANCHORS[i % 2] if len(character_ids) > 1 else "center"
                character_layout.append(
                    PanelCharacter(
                        id=character_id,
                        position=position,
                        expression=directive.expression if directive else "",
                        action=directive.action if directive else "",
                        region_box=(
                            directive.region_box
                            if directive and directive.region_box is not None
                            else default_region_box(position, i, len(character_ids))
                        ),
                    )
                )
            dialogues = []
            for dialogue_index, line in enumerate(script_panel.dialogue):
                if not line.text.strip():
                    continue
                speaker_id = match_character_id(line.speaker, base.characters) or line.speaker
                # 画面内＝話者が描画人物に含まれ、kindが会話/叫びのとき。ナレーションは常に画面外。
                on_screen = (
                    line.kind != "narration" and line.on_screen and speaker_id in character_ids
                )
                dialogues.append(
                    Dialogue(
                        speaker=speaker_id,
                        text=line.text[:120] if line.text else "…",
                        kind=line.kind,
                        on_screen=on_screen,
                        position="upper_right" if dialogue_index % 2 == 0 else "upper_left",
                        vertical=base.typography.vertical_default,
                    )
                )
            panel_bbox = layout[index]
            gen_w, gen_h = compute_generation_size(panel_bbox)
            shape_points = None
            if family in {"action", "reveal"} and index == 0:
                # フロントのslant-rightプリセット定数(0.12/0.88)に揃える。ずれると
                # 編集画面のshapePreset()が右傾斜を認識できず「台形」と表示されてしまう。
                shape_points = [(0.12, 0.0), (1.0, 0.0), (0.88, 1.0), (0.0, 1.0)]
            panels.append(
                Panel(
                    panel_id=panel_id,
                    bbox=panel_bbox,
                    shape_points=shape_points,
                    shot=script_panel.shot or "コマ",
                    subject_mode=subject_mode,
                    role=role,
                    emotion=script_panel.emotion,
                    background_density=script_panel.background_density,
                    composition_notes=script_panel.composition_notes,
                    text_safe_area=script_panel.text_safe_area,
                    emphasis=layout_engine.derive_emphasis(role),
                    camera=script_panel.camera,
                    location_id=resolve_location_id(script_panel.location, base),
                    characters=character_ids,
                    character_layout=character_layout,
                    prompt=script_panel.visual_prompt,
                    dialogue=dialogues,
                    sfx=build_panel_sfx(script_panel),
                    generation=GenerationInfo(
                        backend="stub",
                        prompt=script_panel.visual_prompt,
                        negative_prompt=common_negative,
                        seed=script_page.page * 100 + index + 1,
                        width=gen_w,
                        height=gen_h,
                        status="pending",
                    ),
                )
            )
        pages.append(
            Page(
                page=script_page.page,
                theme=f"{script_page.page}ページ",
                layout_template=f"count_{panel_count}",
                layout_family=family,
                page_goal=script_page.page_goal,
                emotional_curve=script_page.emotional_curve,
                reading_order=[panel.panel_id for panel in panels],
                panels=panels,
            )
        )

    return MangaProject(
        title=base.title,
        work_name=base.work_name,
        premise=base.premise,
        target_pages=len(pages),
        reading_direction=base.reading_direction,
        typography=base.typography.model_copy(deep=True),
        page_layout=base.page_layout.model_copy(deep=True),
        common_positive_prompt=common_positive,
        common_negative_prompt=common_negative,
        characters=base.characters,
        locations=base.locations,
        workflow_presets=base.workflow_presets,
        active_workflow_preset_id=base.active_workflow_preset_id,
        pages=pages,
    )


def apply_session(
    session: Session,
    record: StoryGenerationSessionRecord,
    base: MangaProject,
) -> MangaProject:
    stages = load_stages(record)
    if stages["script"]["data"] is None:
        raise StoryError("台本段階を生成してから適用してください")
    script = ScriptStage.model_validate(stages["script"]["data"])
    if len(script.pages) not in {4, 8, 16}:
        raise StoryError("ページ数は4・8・16のいずれかにしてください")
    # 知識DBのキャラ画像情報(trigger_promptなど)をCharacterプロファイルへ反映する。
    merge_knowledge_characters(base, build_characters_from_knowledge(session, record.work_name))
    # 台詞の無いコマ向けに、ページ構成段階の登場人物をフォールバックとして渡す。
    pages_data = stages["pages"].get("data") or {}
    page_characters = {
        int(outline.get("page", 0)): list(outline.get("characters", []) or [])
        for outline in pages_data.get("pages", [])
        if outline.get("page")
    }
    unresolved: list[str] = []
    manga = script_to_manga(script, base, page_characters, warnings=unresolved)
    # 解決不能な人物名は台本段階の警告として残し、StoryPanelで利用者に知らせる。
    # 安定prefixの警告だけを毎回再計算・置換し、修正後の再適用で古い誤字警告を消す。
    existing = list(stages["script"].get("warnings") or [])
    updated = merge_unresolved_warnings(existing, unresolved)
    if updated != existing:
        stages["script"]["warnings"] = updated
        save_stages(session, record, stages)
    return manga


def restore_revision(manga_json: str) -> MangaProject:
    return MangaProject.model_validate(json.loads(manga_json))
