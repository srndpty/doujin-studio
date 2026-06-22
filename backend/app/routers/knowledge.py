"""作品知識DBのHTTPルーター。"""

from fastapi import APIRouter, HTTPException, Request

from .. import knowledge as knowledge_db
from ..schemas import (
    KnowledgeDocumentRequest,
    KnowledgeImportRequest,
    KnowledgeImportResponse,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceResponse,
    LocalKnowledgeSyncResponse,
    LocalKnowledgeWorkResponse,
)
from .common import to_knowledge_chunk, to_knowledge_source

router = APIRouter()


@router.get("/api/knowledge/local-works", response_model=list[LocalKnowledgeWorkResponse])
def list_local_knowledge_works(request: Request) -> list[LocalKnowledgeWorkResponse]:
    try:
        works = knowledge_db.list_local_works(request.app.state.settings.knowledge_dir)
    except knowledge_db.LocalKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        LocalKnowledgeWorkResponse(
            work_id=work.work_id,
            work_name=work.work_name,
            description=work.description,
            document_count=len(work.documents),
        )
        for work in works
    ]


@router.post(
    "/api/knowledge/local-works/{work_id}/sync",
    response_model=LocalKnowledgeSyncResponse,
)
def sync_local_knowledge_work(work_id: str, request: Request) -> LocalKnowledgeSyncResponse:
    try:
        work = knowledge_db.load_local_work(request.app.state.settings.knowledge_dir, work_id)
        with request.app.state.SessionLocal() as session:
            records = knowledge_db.sync_local_work(session, work)
            sources = [to_knowledge_source(record) for record in records]
    except knowledge_db.LocalKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LocalKnowledgeSyncResponse(
        work=LocalKnowledgeWorkResponse(
            work_id=work.work_id,
            work_name=work.work_name,
            description=work.description,
            document_count=len(work.documents),
        ),
        sources=sources,
    )


@router.post("/api/knowledge/sources/import", response_model=KnowledgeImportResponse)
def import_knowledge_sources(
    payload: KnowledgeImportRequest, request: Request
) -> KnowledgeImportResponse:
    sources: list[KnowledgeSourceResponse] = []
    with request.app.state.SessionLocal() as session:
        for file in payload.files:
            record = knowledge_db.import_source(
                session,
                work_name=payload.work_name,
                title=file.filename,
                doc_type=knowledge_db.infer_doc_type(file.filename),
                usage=payload.usage,
                content=file.content,
            )
            sources.append(to_knowledge_source(record))
        session.commit()
    return KnowledgeImportResponse(sources=sources)


@router.post("/api/knowledge/documents", response_model=KnowledgeSourceResponse)
def add_knowledge_document(
    payload: KnowledgeDocumentRequest, request: Request
) -> KnowledgeSourceResponse:
    with request.app.state.SessionLocal() as session:
        record = knowledge_db.import_source(
            session,
            work_name=payload.work_name,
            title=payload.title or "無題ドキュメント",
            doc_type=payload.doc_type,
            usage=payload.usage,
            content=payload.content,
        )
        session.commit()
        return to_knowledge_source(record)


@router.get("/api/knowledge/sources", response_model=list[KnowledgeSourceResponse])
def list_knowledge_sources(
    request: Request, work_name: str | None = None
) -> list[KnowledgeSourceResponse]:
    with request.app.state.SessionLocal() as session:
        return [
            to_knowledge_source(record) for record in knowledge_db.list_sources(session, work_name)
        ]


@router.delete("/api/knowledge/sources/{source_id}")
def delete_knowledge_source(source_id: str, request: Request) -> dict[str, bool]:
    with request.app.state.SessionLocal() as session:
        if not knowledge_db.delete_source(session, source_id):
            raise HTTPException(status_code=404, detail="知識ソースが見つかりません")
        session.commit()
    return {"ok": True}


@router.post("/api/knowledge/search", response_model=KnowledgeSearchResponse)
def search_knowledge(payload: KnowledgeSearchRequest, request: Request) -> KnowledgeSearchResponse:
    with request.app.state.SessionLocal() as session:
        hits = knowledge_db.search_chunks(
            session,
            work_name=payload.work_name,
            query=payload.query,
            usage=payload.usage,
            limit=payload.limit,
        )
        return KnowledgeSearchResponse(
            hits=[
                KnowledgeSearchHit.model_validate(
                    {"chunk": to_knowledge_chunk(record), "score": score, "method": method}
                )
                for record, score, method in hits
            ]
        )
