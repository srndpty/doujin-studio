from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import database
from .database import KnowledgeChunkRecord, KnowledgeSourceRecord, now_utc

TXT_CHUNK_CHARS = 800


@dataclass
class ChunkData:
    content: str
    kind: str = ""
    title: str = ""
    policy: str = ""
    tags: list[str] = field(default_factory=list)


def infer_doc_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".json"):
        return "json"
    if lowered.endswith((".md", ".markdown")):
        return "markdown"
    return "txt"


def normalize_tags(value) -> list[str]:
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(",") if tag.strip()]
    if isinstance(value, (list, tuple)):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    return []


def chunk_document(doc_type: str, content: str, default_title: str = "") -> list[ChunkData]:
    if doc_type == "json":
        return chunk_json(content, default_title)
    if doc_type == "markdown":
        return chunk_markdown(content, default_title)
    return chunk_txt(content, default_title)


def chunk_json(content: str, default_title: str) -> list[ChunkData]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return chunk_txt(content, default_title)
    if isinstance(parsed, list):
        chunks = [chunk_from_json_object(item, default_title) for item in parsed]
        return [chunk for chunk in chunks if chunk.content]
    if isinstance(parsed, dict):
        if any(key in parsed for key in ("kind", "title", "content", "policy", "tags")):
            chunk = chunk_from_json_object(parsed, default_title)
            return [chunk] if chunk.content else []
        documents = parsed.get("documents") or parsed.get("items")
        if isinstance(documents, list):
            chunks = [chunk_from_json_object(item, default_title) for item in documents]
            return [chunk for chunk in chunks if chunk.content]
    return chunk_txt(json.dumps(parsed, ensure_ascii=False, indent=2), default_title)


def chunk_from_json_object(item, default_title: str) -> ChunkData:
    if not isinstance(item, dict):
        return ChunkData(content=str(item).strip(), title=default_title)
    raw_content = item.get("content", "")
    if isinstance(raw_content, (dict, list)):
        body = json.dumps(raw_content, ensure_ascii=False, indent=2)
    else:
        body = str(raw_content).strip()
    return ChunkData(
        content=body,
        kind=str(item.get("kind", "")).strip(),
        title=str(item.get("title", "") or default_title).strip(),
        policy=str(item.get("policy", "")).strip(),
        tags=normalize_tags(item.get("tags")),
    )


def chunk_markdown(content: str, default_title: str) -> list[ChunkData]:
    lines = content.splitlines()
    chunks: list[ChunkData] = []
    current_title = default_title
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            chunks.append(ChunkData(content=body, title=current_title.strip()))

    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            buffer = []
            current_title = heading.group(2)
        else:
            buffer.append(line)
    flush()
    if not chunks:
        return chunk_txt(content, default_title)
    return chunks


def chunk_txt(content: str, default_title: str, chunk_chars: int = TXT_CHUNK_CHARS) -> list[ChunkData]:
    text_content = content.strip()
    if not text_content:
        return []
    chunks: list[ChunkData] = []
    for index in range(0, len(text_content), chunk_chars):
        segment = text_content[index : index + chunk_chars].strip()
        if segment:
            suffix = f" #{index // chunk_chars + 1}" if len(text_content) > chunk_chars else ""
            chunks.append(ChunkData(content=segment, title=f"{default_title}{suffix}".strip()))
    return chunks


def import_source(
    session: Session,
    *,
    work_name: str,
    title: str,
    doc_type: str,
    usage: str,
    content: str,
) -> KnowledgeSourceRecord:
    chunks = chunk_document(doc_type, content, default_title=title)
    source = KnowledgeSourceRecord(
        id=str(uuid.uuid4()),
        work_name=work_name,
        title=title,
        doc_type=doc_type,
        usage=usage,
        chunk_count=len(chunks),
        created_at=now_utc(),
    )
    session.add(source)
    for position, chunk in enumerate(chunks):
        record = KnowledgeChunkRecord(
            id=str(uuid.uuid4()),
            source_id=source.id,
            work_name=work_name,
            usage=usage,
            kind=chunk.kind,
            title=chunk.title,
            content=chunk.content,
            policy=chunk.policy,
            tags=", ".join(chunk.tags),
            position=position,
        )
        session.add(record)
        insert_fts(session, record)
    session.commit()
    session.refresh(source)
    return source


def insert_fts(session: Session, record: KnowledgeChunkRecord) -> None:
    if not database.FTS5_AVAILABLE:
        return
    session.execute(
        text(
            "INSERT INTO knowledge_chunks_fts (chunk_id, title, content, tags) "
            "VALUES (:chunk_id, :title, :content, :tags)"
        ),
        {"chunk_id": record.id, "title": record.title, "content": record.content, "tags": record.tags},
    )


def delete_source(session: Session, source_id: str) -> bool:
    source = session.get(KnowledgeSourceRecord, source_id)
    if source is None:
        return False
    chunk_ids = [
        row[0]
        for row in session.query(KnowledgeChunkRecord.id).filter(KnowledgeChunkRecord.source_id == source_id).all()
    ]
    if database.FTS5_AVAILABLE and chunk_ids:
        session.execute(
            text("DELETE FROM knowledge_chunks_fts WHERE chunk_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": chunk_ids},
        )
    session.query(KnowledgeChunkRecord).filter(KnowledgeChunkRecord.source_id == source_id).delete()
    session.delete(source)
    session.commit()
    return True


def list_sources(session: Session, work_name: str | None = None) -> list[KnowledgeSourceRecord]:
    query = session.query(KnowledgeSourceRecord)
    if work_name:
        query = query.filter(KnowledgeSourceRecord.work_name == work_name)
    return query.order_by(KnowledgeSourceRecord.created_at.desc()).all()


def get_required_chunks(session: Session, work_name: str) -> list[KnowledgeChunkRecord]:
    return (
        session.query(KnowledgeChunkRecord)
        .filter(KnowledgeChunkRecord.work_name == work_name, KnowledgeChunkRecord.usage == "required")
        .order_by(KnowledgeChunkRecord.source_id, KnowledgeChunkRecord.position)
        .all()
    )


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_chunks(
    session: Session,
    *,
    work_name: str,
    query: str,
    usage: str | None = None,
    limit: int = 10,
) -> list[tuple[KnowledgeChunkRecord, float, str]]:
    query = query.strip()
    results: list[tuple[KnowledgeChunkRecord, float, str]] = []
    seen: set[str] = set()

    if len(query) >= 3 and database.FTS5_AVAILABLE:
        match_term = '"' + query.replace('"', '""') + '"'
        rows = session.execute(
            text(
                "SELECT chunk_id, rank FROM knowledge_chunks_fts "
                "WHERE knowledge_chunks_fts MATCH :term ORDER BY rank LIMIT :n"
            ),
            {"term": match_term, "n": limit * 4},
        ).all()
        for chunk_id, rank in rows:
            record = session.get(KnowledgeChunkRecord, chunk_id)
            if record is None or record.work_name != work_name:
                continue
            if usage and record.usage != usage:
                continue
            if record.id in seen:
                continue
            seen.add(record.id)
            results.append((record, float(-rank), "trigram"))
            if len(results) >= limit:
                return results

    # 短い語やtrigramで拾えない語をLIKEで補完する。
    like_query = session.query(KnowledgeChunkRecord).filter(KnowledgeChunkRecord.work_name == work_name)
    if usage:
        like_query = like_query.filter(KnowledgeChunkRecord.usage == usage)
    pattern = f"%{escape_like(query)}%"
    like_query = like_query.filter(
        KnowledgeChunkRecord.content.like(pattern, escape="\\")
        | KnowledgeChunkRecord.title.like(pattern, escape="\\")
        | KnowledgeChunkRecord.tags.like(pattern, escape="\\")
    )
    for record in like_query.order_by(KnowledgeChunkRecord.source_id, KnowledgeChunkRecord.position).limit(limit * 4):
        if record.id in seen:
            continue
        seen.add(record.id)
        score = float(record.content.count(query) + record.title.count(query) + record.tags.count(query))
        results.append((record, score, "like"))
        if len(results) >= limit:
            break
    return results[:limit]
