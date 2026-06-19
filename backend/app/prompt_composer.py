from __future__ import annotations

from .schemas import MangaProject, Panel


def compose_panel_prompts(manga: MangaProject, panel: Panel) -> tuple[str, str]:
    characters_by_id = {character.id: character for character in manga.characters}
    positive_parts = [manga.common_positive_prompt]
    negative_parts = [manga.common_negative_prompt]

    for character_id in panel.characters:
        character = characters_by_id.get(character_id)
        if character is None:
            continue
        positive_parts.extend(
            [
                character.trigger_prompt or character.display_name,
                character.appearance_prompt,
                character.outfit_prompt,
            ]
        )
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
    return prepared
