from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import database
from .database import KnowledgeChunkRecord, KnowledgeSourceRecord, now_utc

TXT_CHUNK_CHARS = 800
LOCAL_SOURCE_PREFIX = "local:"


@dataclass
class ChunkData:
    content: str
    kind: str = ""
    title: str = ""
    policy: str = ""
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LocalKnowledgeDocument:
    path: Path
    usage: str


@dataclass(frozen=True)
class LocalKnowledgeWork:
    work_id: str
    work_name: str
    description: str
    documents: tuple[LocalKnowledgeDocument, ...]


class LocalKnowledgeError(ValueError):
    pass


def load_local_work(knowledge_dir: Path, work_id: str) -> LocalKnowledgeWork:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", work_id):
        raise LocalKnowledgeError("作品IDの形式が不正です")
    pack_dir = (knowledge_dir / work_id).resolve()
    root = knowledge_dir.resolve()
    if pack_dir.parent != root:
        raise LocalKnowledgeError("作品ディレクトリが不正です")
    manifest_path = pack_dir / "work.json"
    if not manifest_path.is_file():
        raise LocalKnowledgeError(f"作品定義がありません: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalKnowledgeError(f"作品定義を読み込めません: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise LocalKnowledgeError("work.jsonはJSONオブジェクトにしてください")
    manifest_work_id = str(manifest.get("work_id", work_id)).strip()
    work_name = str(manifest.get("work_name", "")).strip()
    if manifest_work_id != work_id or not work_name:
        raise LocalKnowledgeError("work_idまたはwork_nameが不正です")
    raw_documents = manifest.get("documents", [])
    if not isinstance(raw_documents, list) or not raw_documents:
        raise LocalKnowledgeError("documentsを1件以上指定してください")
    documents: list[LocalKnowledgeDocument] = []
    for item in raw_documents:
        if not isinstance(item, dict):
            raise LocalKnowledgeError("documentsの要素はJSONオブジェクトにしてください")
        relative = Path(str(item.get("file", "")))
        usage = str(item.get("usage", "reference"))
        if usage not in {"required", "reference"}:
            raise LocalKnowledgeError("usageはrequiredまたはreferenceにしてください")
        document_path = (pack_dir / relative).resolve()
        if document_path.parent != pack_dir or document_path.suffix.lower() != ".json":
            raise LocalKnowledgeError("知識ファイルは作品ディレクトリ直下のJSONにしてください")
        if not document_path.is_file():
            raise LocalKnowledgeError(f"知識ファイルがありません: {relative}")
        documents.append(LocalKnowledgeDocument(path=document_path, usage=usage))
    return LocalKnowledgeWork(
        work_id=work_id,
        work_name=work_name,
        description=str(manifest.get("description", "")).strip(),
        documents=tuple(documents),
    )


def list_local_works(knowledge_dir: Path) -> list[LocalKnowledgeWork]:
    if not knowledge_dir.is_dir():
        return []
    works: list[LocalKnowledgeWork] = []
    for directory in sorted(knowledge_dir.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or not (directory / "work.json").is_file():
            continue
        works.append(load_local_work(knowledge_dir, directory.name))
    return works


def sync_local_work(session: Session, work: LocalKnowledgeWork) -> list[KnowledgeSourceRecord]:
    marker = f"{LOCAL_SOURCE_PREFIX}{work.work_id}:"
    # まず全ファイルを読み込み・パースしてから一括で差し替える。途中失敗時は
    # rollbackして旧データを保持し、「旧データは消えたが新データは一部だけ」を防ぐ。
    documents: list[tuple[str, str]] = []
    for document in work.documents:
        try:
            content = document.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LocalKnowledgeError(
                f"知識ファイルを読み込めません: {document.path.name}"
            ) from exc
        documents.append((f"{marker}{document.path.name}", content))

    existing = (
        session.query(KnowledgeSourceRecord)
        .filter(KnowledgeSourceRecord.title.like(f"{escape_like(marker)}%", escape="\\"))
        .all()
    )
    try:
        for source in existing:
            delete_source(session, source.id)
        imported: list[KnowledgeSourceRecord] = [
            import_source(
                session,
                work_name=work.work_name,
                title=title,
                doc_type="json",
                usage=document.usage,
                content=content,
            )
            for (title, content), document in zip(documents, work.documents, strict=True)
        ]
        session.commit()
    except Exception:
        session.rollback()
        raise
    return imported


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
    image = item.get("image")
    meta = {"image": image} if isinstance(image, dict) else {}
    return ChunkData(
        content=body,
        kind=str(item.get("kind", "")).strip(),
        title=str(item.get("title", "") or default_title).strip(),
        policy=str(item.get("policy", "")).strip(),
        tags=normalize_tags(item.get("tags")),
        meta=meta,
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


def chunk_txt(
    content: str, default_title: str, chunk_chars: int = TXT_CHUNK_CHARS
) -> list[ChunkData]:
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
    # 親(source)を先にflushしてからチャンクを追加する。SQLAlchemyはrelationship無しの
    # 単なるForeignKeyではinsert順を保証しないため、foreign_keys=ON下でFK違反になる。
    # commitはせず呼び出し側のトランザクションに委ね、複数ソース取り込みを原子的にする。
    session.flush()
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
            meta=json.dumps(chunk.meta, ensure_ascii=False) if chunk.meta else "",
            position=position,
        )
        session.add(record)
        insert_fts(session, record)
    # autoflush=Off環境でも、同一セッションの後続クエリ(検索など)が取り込んだ
    # チャンクを参照できるようflushしておく（commitは呼び出し側）。
    session.flush()
    return source


def insert_fts(session: Session, record: KnowledgeChunkRecord) -> None:
    if not database.FTS5_AVAILABLE:
        return
    session.execute(
        text(
            "INSERT INTO knowledge_chunks_fts (chunk_id, title, content, tags) "
            "VALUES (:chunk_id, :title, :content, :tags)"
        ),
        {
            "chunk_id": record.id,
            "title": record.title,
            "content": record.content,
            "tags": record.tags,
        },
    )


def delete_source(session: Session, source_id: str) -> bool:
    source = session.get(KnowledgeSourceRecord, source_id)
    if source is None:
        return False
    chunk_ids = [
        row[0]
        for row in session.query(KnowledgeChunkRecord.id)
        .filter(KnowledgeChunkRecord.source_id == source_id)
        .all()
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
    # commitは呼び出し側に委ねる（同期処理での原子的な差し替えを壊さないため）。
    session.flush()
    return True


def list_sources(session: Session, work_name: str | None = None) -> list[KnowledgeSourceRecord]:
    query = session.query(KnowledgeSourceRecord)
    if work_name:
        query = query.filter(KnowledgeSourceRecord.work_name == work_name)
    return query.order_by(KnowledgeSourceRecord.created_at.desc()).all()


def get_character_chunks(session: Session, work_name: str) -> list[KnowledgeChunkRecord]:
    """作品のキャラクター種別チャンクをrequired優先で返す。"""
    if not work_name:
        return []
    return (
        session.query(KnowledgeChunkRecord)
        .filter(
            KnowledgeChunkRecord.work_name == work_name, KnowledgeChunkRecord.kind == "character"
        )
        .order_by(
            KnowledgeChunkRecord.usage,
            KnowledgeChunkRecord.source_id,
            KnowledgeChunkRecord.position,
        )
        .all()
    )


def parse_chunk_image(chunk: KnowledgeChunkRecord) -> dict:
    """チャンクのmetaから画像生成用情報(image)を取り出す。無ければ空dict。"""
    if not getattr(chunk, "meta", ""):
        return {}
    try:
        meta = json.loads(chunk.meta)
    except (json.JSONDecodeError, TypeError):
        return {}
    image = meta.get("image") if isinstance(meta, dict) else None
    return image if isinstance(image, dict) else {}


def get_required_chunks(session: Session, work_name: str) -> list[KnowledgeChunkRecord]:
    return (
        session.query(KnowledgeChunkRecord)
        .filter(
            KnowledgeChunkRecord.work_name == work_name, KnowledgeChunkRecord.usage == "required"
        )
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
    like_query = session.query(KnowledgeChunkRecord).filter(
        KnowledgeChunkRecord.work_name == work_name
    )
    if usage:
        like_query = like_query.filter(KnowledgeChunkRecord.usage == usage)
    pattern = f"%{escape_like(query)}%"
    like_query = like_query.filter(
        KnowledgeChunkRecord.content.like(pattern, escape="\\")
        | KnowledgeChunkRecord.title.like(pattern, escape="\\")
        | KnowledgeChunkRecord.tags.like(pattern, escape="\\")
    )
    for record in like_query.order_by(
        KnowledgeChunkRecord.source_id, KnowledgeChunkRecord.position
    ).limit(limit * 4):
        if record.id in seen:
            continue
        seen.add(record.id)
        score = float(
            record.content.count(query) + record.title.count(query) + record.tags.count(query)
        )
        results.append((record, score, "like"))
        if len(results) >= limit:
            break
    return results[:limit]
