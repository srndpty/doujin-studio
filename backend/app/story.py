from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field

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
    PlotStage,
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
    session.commit()
    session.refresh(record)
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


def require_previous_approved(stage: str, stages: dict) -> None:
    index = STAGE_ORDER.index(stage)
    if index == 0:
        return
    previous = STAGE_ORDER[index - 1]
    if stages[previous]["status"] != "approved":
        raise StoryError(f"前段階「{previous}」を承認してから生成してください")


def invalidate_downstream(stages: dict, stage: str) -> None:
    index = STAGE_ORDER.index(stage)
    for downstream in STAGE_ORDER[index + 1 :]:
        if stages[downstream]["status"] != "empty":
            stages[downstream]["status"] = "draft"


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
    require_previous_approved(stage, stages)
    context = build_context(session, settings, record.work_name, stage_query(stage, record, stages))

    error: str | None = None
    try:
        if isinstance(llm, StubLLMClient):
            data = generate_stub_stage(session, record, stages, stage)
            data = validate_stage_data(stage, data, record.target_pages)
        else:
            data = await generate_llm_stage(llm, record, stages, stage, context, instruction)
    except StoryError:
        raise
    except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)
        data = None

    stages[stage]["data"] = data
    stages[stage]["knowledge_ids"] = context.knowledge_ids
    stages[stage]["error"] = error
    stages[stage]["status"] = "draft" if data is not None else "empty"
    stages[stage]["updated_at"] = now_utc().isoformat()
    invalidate_downstream(stages, stage)
    save_stages(session, record, stages)
    return record


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
    require_previous_approved(stage, stages)
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
            "出力形式: pages配列。各ページは page(整数) と panels(1〜9個)を持ちます。"
            "各コマは shot, camera, location, visual_prompt, characters([string]), dialogue([{speaker, text}]), sfx([string]) を持ちます。"
            "charactersにはそのコマに描かれる登場人物名を、台詞が無いコマでも必ず列挙してください。"
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
    if retry_error:
        parts.append(
            "前回の出力は検証に失敗しました。次のエラーを修正し、有効なJSONのみを再出力してください:\n"
            + retry_error
        )
    if directive:
        parts.append(
            "前回の出力には次の問題がありました。修正して再出力してください:\n" + directive
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


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


async def generate_llm_stage(
    llm,
    record: StoryGenerationSessionRecord,
    stages: dict,
    stage: str,
    context: KnowledgeContext,
    instruction: str,
) -> dict:
    async def attempt(directive: str | None) -> dict:
        messages = build_stage_messages(
            record, stages, stage, context, instruction, directive=directive
        )
        last_error: Exception | None = None
        for retry in range(2):
            content = await llm.chat(messages, want_json=True)
            try:
                parsed = extract_json_object(content)
                return validate_stage_data(stage, parsed, record.target_pages)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if retry == 0:
                    # Pydantic検証失敗時はエラー内容付きで1回だけ修正要求する。
                    messages = build_stage_messages(
                        record,
                        stages,
                        stage,
                        context,
                        instruction,
                        retry_error=str(exc),
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


def _is_weak_trigger(character: Character) -> bool:
    """画像生成トークンとして弱い（未設定/表示名そのまま）triggerかどうか。"""
    trigger = (character.trigger_prompt or "").strip()
    return not trigger or trigger == character.display_name.strip()


def build_characters_from_knowledge(session: Session, work_name: str) -> list[Character]:
    """知識DBのキャラクター種別チャンクからCharacterプロファイルを生成する。"""
    characters: list[Character] = []
    used_ids: set[str] = set()
    for index, chunk in enumerate(knowledge.get_character_chunks(session, work_name)):
        display_name = (chunk.title or "").strip()
        if not display_name:
            continue
        image = knowledge.parse_chunk_image(chunk)
        char_id = str(image.get("id") or f"kc_{index + 1}").strip() or f"kc_{index + 1}"
        while char_id in used_ids:
            char_id = f"{char_id}_x"
        used_ids.add(char_id)
        aliases = [str(alias).strip() for alias in image.get("aliases", []) if str(alias).strip()]
        characters.append(
            Character(
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
        )
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


def script_to_manga(
    script: ScriptStage,
    base: MangaProject,
    page_characters: dict[int, list[str]] | None = None,
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
            role = layout_engine.derive_role(
                index, panel_count, page_index, total_pages, bool(script_panel.dialogue)
            )
            # コマ明記の登場人物 + 台詞話者を統合し、無ければページ構成の登場人物で補う。
            names: list[str] = list(script_panel.characters)
            for line in script_panel.dialogue:
                if line.speaker and line.speaker not in names:
                    names.append(line.speaker)
            character_ids = resolve_character_ids(names, base)
            if not character_ids:
                character_ids = resolve_character_ids(
                    page_characters.get(script_page.page, []), base
                )
            dialogues = []
            for dialogue_index, line in enumerate(script_panel.dialogue):
                speaker = match_character_id(line.speaker, base.characters) or line.speaker
                dialogues.append(
                    Dialogue(
                        speaker=speaker,
                        text=line.text[:120] if line.text else "…",
                        position="upper_right" if dialogue_index % 2 == 0 else "upper_left",
                        vertical=base.typography.vertical_default,
                    )
                )
            panel_bbox = layout[index]
            gen_w, gen_h = compute_generation_size(panel_bbox)
            panels.append(
                Panel(
                    panel_id=panel_id,
                    bbox=panel_bbox,
                    shot=script_panel.shot or "コマ",
                    role=role,
                    emphasis=layout_engine.derive_emphasis(role),
                    camera=script_panel.camera,
                    location_id=resolve_location_id(script_panel.location, base),
                    characters=character_ids,
                    prompt=script_panel.visual_prompt,
                    dialogue=dialogues,
                    sfx=[
                        Sfx(text=item[:40], position="center") for item in script_panel.sfx if item
                    ],
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
    if stages["script"]["status"] != "approved" or stages["script"]["data"] is None:
        raise StoryError("台本段階を承認してから適用してください")
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
    return script_to_manga(script, base, page_characters)


def restore_revision(manga_json: str) -> MangaProject:
    return MangaProject.model_validate(json.loads(manga_json))
