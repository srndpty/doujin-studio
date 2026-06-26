from __future__ import annotations

from .schemas import LoRABinding, MangaProject, Panel, ReferenceImageBinding

# キャラの同一性（LoRA・外見prompt）を抑制する主題モード。
NON_CHARACTER_SUBJECT_MODES = {"prop_insert", "hand_insert", "background"}
# 小物・手アップでキャラが映り込むのを避けるための除外プロンプト。
INSERT_NEGATIVE_PROMPT = (
    "character print on product, anime character on object, face on item, "
    "full body character, person in frame"
)
SUBJECT_MODE_POSITIVE = {
    "prop_insert": "close-up of a single object, product shot, no people",
    "hand_insert": "close-up of hands, detailed hands, no face",
    "background": "scenery, empty background, establishing shot, no people",
}
# character_layout.positionをプロンプトの位置語へ写像する（通常promptでの大まかな配置）。
POSITION_PHRASE = {
    "upper_left": "on the upper left",
    "upper_right": "on the upper right",
    "lower_left": "on the lower left",
    "lower_right": "on the lower right",
    "center": "in the center",
}


def is_non_character_mode(panel: Panel) -> bool:
    return panel.subject_mode in NON_CHARACTER_SUBJECT_MODES


def compose_panel_prompts(manga: MangaProject, panel: Panel) -> tuple[str, str]:
    characters_by_id = {character.id: character for character in manga.characters}
    positive_parts = [manga.common_positive_prompt]
    negative_parts = [manga.common_negative_prompt]
    location = next((item for item in manga.locations if item.id == panel.location_id), None)
    if location:
        positive_parts.append(location.prompt)
        negative_parts.append(location.negative_prompt)

    non_character = is_non_character_mode(panel)
    if non_character:
        # 小物・手・背景コマではキャラ同一性を入れず、映り込みをnegativeで抑える。
        positive_parts.append(SUBJECT_MODE_POSITIVE.get(panel.subject_mode, ""))
        negative_parts.append(INSERT_NEGATIVE_PROMPT)
    else:
        layout_by_id = {entry.id: entry for entry in panel.character_layout}
        for character_id in panel.characters:
            character = characters_by_id.get(character_id)
            if character is None:
                continue
            # 人物ごとに位置・外見・衣装・表情・動作を隣接させてブロック化し、
            # 表情/動作が別人へ混ざりにくいようにまとめる（通常promptでの近接配置。
            # 厳密な領域分離はregional conditioning workflowが要る）。
            entry = layout_by_id.get(character_id)
            char_tokens = [
                character.trigger_prompt or character.display_name,
                character.appearance_prompt,
                character.outfit_prompt,
            ]
            if entry is not None:
                char_tokens.insert(0, POSITION_PHRASE.get(entry.position, ""))
                char_tokens.extend([entry.expression, entry.action])
            positive_parts.extend(char_tokens)
            negative_parts.append(character.negative_prompt)

    positive_parts.append(panel.generation.prompt or panel.prompt)
    negative_parts.append(panel.generation.negative_prompt)
    return merge_prompt_parts(positive_parts), merge_prompt_parts(negative_parts)


def merge_prompt_parts(parts: list[str]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for tag in part.split(","):
            cleaned = tag.strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                result.append(cleaned)
                seen.add(key)
    return ", ".join(result)


def prepare_panel_for_generation(manga: MangaProject, panel: Panel) -> Panel:
    prepared = panel.model_copy(deep=True)
    positive, negative = compose_panel_prompts(manga, panel)
    prepared.generation.prompt = positive
    prepared.generation.negative_prompt = negative
    characters_by_id = {character.id: character for character in manga.characters}
    prepared.generation.loras = []
    prepared.generation.reference_images = []
    preset_id = panel.generation.workflow_preset_id or manga.active_workflow_preset_id
    prepared.generation.workflow_preset_id = preset_id
    prepared.generation.workflow_preset = next(
        (
            preset.model_copy(deep=True)
            for preset in manga.workflow_presets
            if preset.id == preset_id
        ),
        None,
    )
    if is_non_character_mode(panel):
        # 小物・手・背景コマではキャラLoRA/参照画像を適用しない。
        location = next((item for item in manga.locations if item.id == panel.location_id), None)
        if location and location.reference_load_node_id and location.reference_image_asset:
            prepared.generation.reference_images.append(
                ReferenceImageBinding(
                    node_id=location.reference_load_node_id,
                    asset=location.reference_image_asset,
                    kind="location",
                )
            )
        for control in panel.control_references:
            if control.asset and control.load_node_id:
                prepared.generation.reference_images.append(
                    ReferenceImageBinding(
                        node_id=control.load_node_id, asset=control.asset, kind=control.kind
                    )
                )
        return prepared
    for character_id in panel.characters:
        character = characters_by_id.get(character_id)
        if character is None:
            continue
        if character.lora_node_id and character.lora_name:
            prepared.generation.loras.append(
                LoRABinding(
                    node_id=character.lora_node_id,
                    lora_name=character.lora_name,
                    strength_model=character.lora_strength_model,
                    strength_clip=character.lora_strength_clip,
                )
            )
        if character.reference_load_node_id and character.reference_image_asset:
            prepared.generation.reference_images.append(
                ReferenceImageBinding(
                    node_id=character.reference_load_node_id,
                    asset=character.reference_image_asset,
                    character_id=character.id,
                    kind="character",
                )
            )
    location = next((item for item in manga.locations if item.id == panel.location_id), None)
    if location and location.reference_load_node_id and location.reference_image_asset:
        prepared.generation.reference_images.append(
            ReferenceImageBinding(
                node_id=location.reference_load_node_id,
                asset=location.reference_image_asset,
                kind="location",
            )
        )
    for control in panel.control_references:
        if control.asset and control.load_node_id:
            prepared.generation.reference_images.append(
                ReferenceImageBinding(
                    node_id=control.load_node_id,
                    asset=control.asset,
                    kind=control.kind,
                )
            )
    return prepared
