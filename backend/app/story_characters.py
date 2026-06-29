from __future__ import annotations

from sqlalchemy.orm import Session

from . import knowledge
from .database import KnowledgeChunkRecord
from .schemas import Character, MangaProject


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
