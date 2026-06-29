from __future__ import annotations

import json
import uuid

from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import StoryGenerationSessionRecord, now_utc
from .schemas import (
    BriefStage,
    PagesStage,
    PlotStage,
    ScriptStage,
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


class StoryError(Exception):
    """段階生成の前提条件違反などのユーザー向けエラー。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


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
