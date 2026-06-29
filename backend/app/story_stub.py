from __future__ import annotations

from sqlalchemy.orm import Session

from . import knowledge
from .database import KnowledgeChunkRecord, StoryGenerationSessionRecord
from .schemas import (
    BriefCharacter,
    BriefStage,
    CharacterArc,
    PageOutline,
    PagesStage,
    PlotStage,
    ScriptStage,
)
from .story_stage import StoryError


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
        brief_model = BriefStage(
            synopsis=(record.instruction or f"{work_name}を舞台にした{target}ページの短編。"),
            tone="原作準拠で軽妙",
            characters=[
                BriefCharacter(name=name, role="主役" if index == 0 else "登場人物")
                for index, name in enumerate(names[:4])
            ],
            canon_conditions=canon,
        )
        return brief_model.model_dump()
    if stage == "plot":
        brief_data = stages["brief"].get("data") or {}
        synopsis = brief_data.get("synopsis", f"{work_name}の物語")
        characters = brief_data.get("characters", []) or [{"name": "主役", "role": "主役"}]
        plot_model = PlotStage(
            ki=f"導入: {synopsis}",
            sho="展開: 二人の関係や状況が動き出す。",
            ten="転換: 予想外の出来事で空気が変わる。",
            ketsu="結末: 指定の方向で余韻を残して締める。",
            beats=[f"ビート{index + 1}" for index in range(max(target // 2, 2))],
            character_arcs=[
                CharacterArc(name=c.get("name", "主役"), arc="心情が一歩動く") for c in characters
            ],
        )
        return plot_model.model_dump()
    if stage == "pages":
        brief_data = stages["brief"].get("data") or {}
        plot_data = stages["plot"].get("data") or {}
        names = [c.get("name", "主役") for c in brief_data.get("characters", [])] or [
            "主役",
            "相方",
        ]
        beats = plot_data.get("beats", []) or ["導入", "展開", "転換", "結末"]
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
