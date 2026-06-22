"""ストーリー生成セッションとrevision履歴のHTTPルーター。"""

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request

from .. import knowledge as knowledge_db
from .. import story as story_module
from ..database import ProjectRevisionRecord, StoryGenerationSessionRecord, now_utc
from ..llm import build_llm_client
from ..schemas import (
    MangaProject,
    ProjectDetail,
    ProjectRevisionResponse,
    StageGenerateRequest,
    StageUpdateRequest,
    StorySessionCreate,
    StorySessionResponse,
    StorySessionSummary,
)
from ..story import StoryError
from .common import (
    load_project_record,
    to_detail,
    to_story_summary,
)

router = APIRouter()


@router.post("/api/projects/{project_id}/story-sessions", response_model=StorySessionResponse)
def create_story_session(
    project_id: str, payload: StorySessionCreate, request: Request
) -> StorySessionResponse:
    record = load_project_record(request, project_id)
    work_name = payload.work_name or record.work_name
    if payload.knowledge_work_id:
        with request.app.state.SessionLocal() as session:
            try:
                local_work = knowledge_db.load_local_work(
                    request.app.state.settings.knowledge_dir, payload.knowledge_work_id
                )
                knowledge_db.sync_local_work(session, local_work)
            except knowledge_db.LocalKnowledgeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        work_name = local_work.work_name

        def update_work_name(manga: MangaProject) -> None:
            manga.work_name = work_name

        request.app.state.mutation.mutate_local(project_id, update_work_name)
    with request.app.state.SessionLocal() as session:
        story_record = story_module.create_session(
            session,
            project_id=project_id,
            work_name=work_name,
            target_pages=payload.target_pages,
            instruction=payload.instruction,
        )
        return story_module.session_to_response(story_record)


@router.get("/api/projects/{project_id}/story-sessions", response_model=list[StorySessionSummary])
def list_story_sessions(project_id: str, request: Request) -> list[StorySessionSummary]:
    with request.app.state.SessionLocal() as session:
        records = (
            session.query(StoryGenerationSessionRecord)
            .filter(StoryGenerationSessionRecord.project_id == project_id)
            .order_by(StoryGenerationSessionRecord.created_at.desc())
            .all()
        )
        return [to_story_summary(record) for record in records]


@router.get("/api/story-sessions/{session_id}", response_model=StorySessionResponse)
def get_story_session(session_id: str, request: Request) -> StorySessionResponse:
    with request.app.state.SessionLocal() as session:
        record = session.get(StoryGenerationSessionRecord, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
        return story_module.session_to_response(record)


@router.post(
    "/api/story-sessions/{session_id}/stages/{stage}/generate",
    response_model=StorySessionResponse,
)
async def generate_story_stage(
    session_id: str,
    stage: str,
    request: Request,
    payload: Annotated[StageGenerateRequest | None, Body()] = None,
) -> StorySessionResponse:
    settings = request.app.state.settings
    llm = build_llm_client(settings)
    with request.app.state.SessionLocal() as session:
        record = session.get(StoryGenerationSessionRecord, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
        try:
            await story_module.generate_stage(
                session, llm, settings, record, stage, payload.instruction if payload else ""
            )
        except StoryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return story_module.session_to_response(record)


@router.put("/api/story-sessions/{session_id}/stages/{stage}", response_model=StorySessionResponse)
def update_story_stage(
    session_id: str, stage: str, payload: StageUpdateRequest, request: Request
) -> StorySessionResponse:
    with request.app.state.SessionLocal() as session:
        record = session.get(StoryGenerationSessionRecord, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
        try:
            story_module.update_stage(session, record, stage, payload.data)
        except StoryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return story_module.session_to_response(record)


@router.post(
    "/api/story-sessions/{session_id}/stages/{stage}/approve",
    response_model=StorySessionResponse,
)
def approve_story_stage(session_id: str, stage: str, request: Request) -> StorySessionResponse:
    with request.app.state.SessionLocal() as session:
        record = session.get(StoryGenerationSessionRecord, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
        try:
            story_module.approve_stage(session, record, stage)
        except StoryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return story_module.session_to_response(record)


@router.post("/api/story-sessions/{session_id}/apply", response_model=ProjectDetail)
async def apply_story_session(session_id: str, request: Request, revision: int) -> ProjectDetail:
    with request.app.state.SessionLocal() as session:
        record = session.get(StoryGenerationSessionRecord, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
        project_id = record.project_id

    def build_applied(session, base: MangaProject) -> MangaProject:
        current = session.get(StoryGenerationSessionRecord, session_id)
        if current is None:
            raise HTTPException(status_code=404, detail="ストーリーセッションが見つかりません")
        try:
            return story_module.apply_session(session, current, base)
        except StoryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    mutation_result = request.app.state.mutation.replace_with_history(
        project_id,
        build_applied,
        expected_revision=revision,
        history_label=f"ストーリー適用前 ({now_utc().isoformat()})",
    )
    await request.app.state.generation.cancel_before_epoch(
        project_id, mutation_result.project.generation_epoch
    )
    return to_detail(
        load_project_record(request, project_id), request.app.state.settings.export_dir
    )


@router.get("/api/projects/{project_id}/revisions", response_model=list[ProjectRevisionResponse])
def list_project_revisions(project_id: str, request: Request) -> list[ProjectRevisionResponse]:
    load_project_record(request, project_id)
    with request.app.state.SessionLocal() as session:
        records = (
            session.query(ProjectRevisionRecord)
            .filter(ProjectRevisionRecord.project_id == project_id)
            .order_by(ProjectRevisionRecord.created_at.desc())
            .all()
        )
        return [
            ProjectRevisionResponse(
                id=record.id,
                project_id=record.project_id,
                label=record.label,
                created_at=record.created_at,
            )
            for record in records
        ]


@router.post(
    "/api/projects/{project_id}/revisions/{revision_id}/restore",
    response_model=ProjectDetail,
)
async def restore_project_revision(
    project_id: str, revision_id: str, request: Request, revision: int
) -> ProjectDetail:
    load_project_record(request, project_id)

    def build_restored(session, _base: MangaProject) -> MangaProject:
        stored = session.get(ProjectRevisionRecord, revision_id)
        if stored is None or stored.project_id != project_id:
            raise HTTPException(status_code=404, detail="リビジョンが見つかりません")
        return story_module.restore_revision(stored.manga_json)

    mutation_result = request.app.state.mutation.replace_with_history(
        project_id,
        build_restored,
        expected_revision=revision,
        history_label=f"復元前 ({now_utc().isoformat()})",
    )
    await request.app.state.generation.cancel_before_epoch(
        project_id, mutation_result.project.generation_epoch
    )
    return to_detail(
        load_project_record(request, project_id), request.app.state.settings.export_dir
    )
