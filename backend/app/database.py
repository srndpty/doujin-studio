from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Text, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    work_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manga_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # manga_json更新ごとに増える楽観ロック用バージョン。古いrevisionでの保存は409にする。
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ページ構成を全置換する操作(ネーム再生成・ストーリー適用・リビジョン復元)で増える世代番号。
    # 生成ジョブは開始時の世代を保持し、世代が変わったら古いプロンプトの候補混入を防ぐ。
    generation_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GenerationJobRecord(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    panel_id: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="生成待ちです")
    randomize_seed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ComfyUIのprompt_id。キャンセル時にリモート停止(interrupt/queue削除)へ使う。
    prompt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ジョブ開始時のproject世代。候補保存時に現在世代と異なれば破棄する。
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_input_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stages_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectRevisionRecord(Base):
    __tablename__ = "project_revisions"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
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


class SchemaMigrationError(RuntimeError):
    """起動を止める必要があるSQLite schema migrationエラー。"""


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    name: str
    upgrade: Callable[[Connection], None]
    # 親テーブルをcreate-copy-drop-renameで再作成するmigrationだけで指定する。
    # PRAGMA foreign_keysはトランザクション開始前に切り替える必要がある。
    requires_foreign_keys_off: bool = False


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.execute(text(f"PRAGMA table_info({table_name})")).all()
    return {str(row[1]) for row in rows}


def _table_exists(connection: Connection, table_name: str) -> bool:
    row = connection.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type IN ('table', 'virtual table') AND name = :name
            LIMIT 1
            """
        ),
        {"name": table_name},
    ).first()
    return row is not None


def _migration_001_baseline_schema(connection: Connection) -> None:
    """version 1時点の固定DDLを作成し、migration導入前DBをbaselineへ揃える。

    SQLiteは`ALTER TABLE`で既存テーブルへ外部キー制約を追加できない。
    下記DDLのForeignKey/ON DELETE CASCADEは新規作成テーブルにのみ適用され、
    既存DBには導入されない。既存DBへFK・cascadeを入れる
    場合はテーブル再作成マイグレーションが別途必要。

    将来のORM変更に影響されないようBase.metadataは使わない。
    """
    baseline_ddl = (
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            work_name TEXT NOT NULL DEFAULT '',
            manga_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 0,
            generation_epoch INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS generation_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            panel_id TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            current INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            node TEXT,
            message TEXT NOT NULL DEFAULT '生成待ちです',
            randomize_seed INTEGER NOT NULL DEFAULT 0,
            prompt_id TEXT,
            epoch INTEGER NOT NULL DEFAULT 0,
            generation_input_hash TEXT,
            candidate_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_sources (
            id TEXT PRIMARY KEY,
            work_name TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            doc_type TEXT NOT NULL DEFAULT 'txt',
            usage TEXT NOT NULL DEFAULT 'reference',
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
            work_name TEXT NOT NULL,
            usage TEXT NOT NULL DEFAULT 'reference',
            kind TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            policy TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS story_generation_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            work_name TEXT NOT NULL DEFAULT '',
            target_pages INTEGER NOT NULL DEFAULT 4,
            instruction TEXT NOT NULL DEFAULT '',
            stages_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            label TEXT NOT NULL DEFAULT '',
            manga_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL
        )
        """,
    )
    for ddl in baseline_ddl:
        connection.execute(text(ddl))

    indexes = (
        "CREATE INDEX IF NOT EXISTS ix_generation_jobs_project_id ON generation_jobs(project_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_sources_work_name ON knowledge_sources(work_name)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_source_id ON knowledge_chunks(source_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_work_name ON knowledge_chunks(work_name)",
        "CREATE INDEX IF NOT EXISTS ix_story_generation_sessions_project_id ON story_generation_sessions(project_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_revisions_project_id ON project_revisions(project_id)",
    )
    for ddl in indexes:
        connection.execute(text(ddl))

    if _table_exists(connection, "knowledge_chunks"):
        knowledge_columns = _table_columns(connection, "knowledge_chunks")
        if "meta" not in knowledge_columns:
            connection.execute(
                text("ALTER TABLE knowledge_chunks ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
            )

    if _table_exists(connection, "projects"):
        project_columns = _table_columns(connection, "projects")
        if "revision" not in project_columns:
            connection.execute(
                text("ALTER TABLE projects ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            )
        if "generation_epoch" not in project_columns:
            connection.execute(
                text("ALTER TABLE projects ADD COLUMN generation_epoch INTEGER NOT NULL DEFAULT 0")
            )

    if _table_exists(connection, "generation_jobs"):
        job_columns = _table_columns(connection, "generation_jobs")
        if "prompt_id" not in job_columns:
            connection.execute(text("ALTER TABLE generation_jobs ADD COLUMN prompt_id TEXT"))
        if "randomize_seed" not in job_columns:
            connection.execute(
                text(
                    "ALTER TABLE generation_jobs ADD COLUMN randomize_seed INTEGER NOT NULL DEFAULT 0"
                )
            )
        if "epoch" not in job_columns:
            connection.execute(
                text("ALTER TABLE generation_jobs ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0")
            )
        if "generation_input_hash" not in job_columns:
            connection.execute(
                text("ALTER TABLE generation_jobs ADD COLUMN generation_input_hash TEXT")
            )
        # 旧DBにactive重複があれば、一意index導入前に片方だけ残して終端化する。
        connection.execute(
            text(
                """
                UPDATE generation_jobs
                SET status = 'error',
                    message = '重複ジョブをDB移行時に停止しました',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('queued', 'running')
                  AND id NOT IN (
                    SELECT MIN(id)
                    FROM generation_jobs
                    WHERE status IN ('queued', 'running')
                    GROUP BY project_id, panel_id
                  )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_generation_jobs_active_panel
                ON generation_jobs(project_id, panel_id)
                WHERE status IN ('queued', 'running')
                """
            )
        )


MIGRATIONS: tuple[SchemaMigration, ...] = (
    SchemaMigration(1, "baseline_schema", _migration_001_baseline_schema),
)


def _create_schema_migrations_table(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
    )


def run_schema_migrations(engine) -> None:
    """未適用のSQLite schema migrationをversion順に実行する。

    新規DBとschema_migrationsを持たない既存DBのどちらも、同じrunnerでbaseline
    migrationを通して最新化する。各migrationは成功した場合にのみ適用記録を追加する。
    親テーブル再作成はrequires_foreign_keys_offを指定し、commit前にFK整合性を検査する。
    """
    known_versions = [migration.version for migration in MIGRATIONS]
    if known_versions != list(range(1, len(known_versions) + 1)):
        raise SchemaMigrationError("schema migration定義に欠番があります。起動を停止します。")
    migrations_by_version = {migration.version: migration for migration in MIGRATIONS}
    latest_known = known_versions[-1] if known_versions else 0

    # FK無効化が必要なmigrationは、通常接続でpendingを確認した後、専用接続で
    # PRAGMAを切り替えて再度lock・履歴確認する。並行runnerが先に適用した場合は、
    # lock取得後に判明した次migrationの要件に合わせて接続を取り直す。
    planned_migration: SchemaMigration | None = None
    while True:
        migration: SchemaMigration | None = None
        connection = engine.connect()
        foreign_keys_disabled = bool(
            planned_migration and planned_migration.requires_foreign_keys_off
        )
        try:
            if foreign_keys_disabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
                if foreign_keys != 0:
                    raise SchemaMigrationError(
                        "migration用connectionの外部キー制約を無効化できません。起動を停止します。"
                    )
            # sqlite3はDDLだけでは物理トランザクションを開始しないことがある。
            # 書込lockを明示取得してから履歴を読み直し、DDLと履歴INSERTを同時に確定する。
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            _create_schema_migrations_table(connection)
            rows = connection.execute(
                text("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
            ).all()
            applied_versions = [int(row[0]) for row in rows]
            if len(applied_versions) != len(set(applied_versions)):
                raise SchemaMigrationError(
                    "schema_migrationsに重複versionがあります。起動を停止します。"
                )
            if applied_versions:
                unknown_versions = [
                    version for version in applied_versions if version > latest_known
                ]
                if unknown_versions:
                    raise SchemaMigrationError(
                        "このアプリより新しいDB schema versionが適用済みです。"
                        f"version={unknown_versions[0]} のため起動を停止します。"
                    )
                expected_applied = list(range(1, max(applied_versions) + 1))
                if applied_versions != expected_applied:
                    raise SchemaMigrationError(
                        "schema_migrationsに欠番があります。起動を停止します。"
                    )
                unknown_names = [
                    (version, name)
                    for version, name, _applied_at in rows
                    if migrations_by_version[int(version)].name != name
                ]
                if unknown_names:
                    version, name = unknown_names[0]
                    raise SchemaMigrationError(
                        "schema_migrationsに未知のmigration名があります。"
                        f"version={version}, name={name} のため起動を停止します。"
                    )

            applied = set(applied_versions)
            migration = next((item for item in MIGRATIONS if item.version not in applied), None)
            if migration is None:
                # migration導入初期のDBでは、version 1適用済みとして記録された後に
                # baselineの補修対象列が増えた場合がある。履歴は正しくても実テーブルが
                # 古いままなら、ここで不足列だけをidempotentに補う。
                _migration_001_baseline_schema(connection)
                connection.commit()
                return
            if migration.requires_foreign_keys_off != foreign_keys_disabled:
                connection.rollback()
                planned_migration = migration
                continue
            migration.upgrade(connection)
            if foreign_keys_disabled:
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                if violations:
                    table, rowid, parent, foreign_key_id = violations[0]
                    raise SchemaMigrationError(
                        "schema migration後の外部キー整合性検査に失敗しました。"
                        f"table={table}, rowid={rowid}, parent={parent}, fk_id={foreign_key_id}。"
                        "起動を停止します。"
                    )
            connection.execute(
                text(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at)
                    VALUES (:version, :name, :applied_at)
                    """
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "applied_at": now_utc().isoformat(),
                },
            )
            connection.commit()
            planned_migration = None
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, SchemaMigrationError):
                raise
            migration_label = (
                f"{migration.version} ({migration.name})" if migration else "runner初期化"
            )
            raise SchemaMigrationError(
                f"schema migration {migration_label} に失敗しました。起動を停止します。"
            ) from exc
        finally:
            if foreign_keys_disabled:
                # PRAGMA foreign_keysはtransaction終了後に必ず元へ戻す。
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.close()


def create_session_factory(database_url: str) -> sessionmaker:
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    )
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            # 個人用ローカルアプリ向けに、耐障害性と性能のバランスを取る設定。
            # WAL+NORMALはプロセス強制終了やOSクラッシュでもコミット済みデータを保つ。
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    if database_url.startswith("sqlite"):
        run_schema_migrations(engine)
    else:
        Base.metadata.create_all(engine)
    ensure_fts(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
