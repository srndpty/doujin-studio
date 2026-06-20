from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, Text, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    work_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manga_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GenerationJobRecord(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    panel_id: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="生成待ちです")
    candidate_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeSourceRecord(Base):
    __tablename__ = "knowledge_sources"

    id: Mapped[str] = mapped_column(primary_key=True)
    work_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    doc_type: Mapped[str] = mapped_column(Text, nullable=False, default="txt")
    usage: Mapped[str] = mapped_column(Text, nullable=False, default="reference")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeChunkRecord(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    work_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    usage: Mapped[str] = mapped_column(Text, nullable=False, default="reference")
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    policy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 画像生成用などの構造化メタデータをJSON文字列で保持する（例: キャラのtrigger_prompt）。
    meta: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class StoryGenerationSessionRecord(Base):
    __tablename__ = "story_generation_sessions"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    work_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stages_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectRevisionRecord(Base):
    __tablename__ = "project_revisions"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manga_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# FTS5 trigram検索が利用可能か（SQLiteビルド依存）。利用不可ならLIKE検索へ退避する。
FTS5_AVAILABLE = False


def ensure_fts(engine) -> None:
    """knowledge_chunks用のFTS5 trigram索引を作成する。失敗時はLIKE検索へ退避する。"""
    global FTS5_AVAILABLE
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts "
                    "USING fts5(chunk_id UNINDEXED, title, content, tags, tokenize='trigram')"
                )
            )
        FTS5_AVAILABLE = True
    except Exception:
        FTS5_AVAILABLE = False


def ensure_columns(engine) -> None:
    """create_allでは追加されない後付けカラムを既存SQLite DBへ補う。"""
    with engine.begin() as connection:
        rows = connection.execute(text("PRAGMA table_info(knowledge_chunks)")).all()
        columns = {row[1] for row in rows}
        if "meta" not in columns:
            connection.execute(
                text("ALTER TABLE knowledge_chunks ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
            )


def create_session_factory(database_url: str) -> sessionmaker:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=OFF")
            cursor.execute("PRAGMA synchronous=OFF")
            cursor.close()

    Base.metadata.create_all(engine)
    if database_url.startswith("sqlite"):
        ensure_columns(engine)
    ensure_fts(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
