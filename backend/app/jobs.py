from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .database import GenerationJobRecord, ProjectRecord

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "done", "error", "cancelled"]
TERMINAL_JOB_STATUSES = {"done", "error", "cancelled"}
# 終了ジョブをメモリキャッシュに残す猶予（UI通知・直後の再取得用）。
# これ以降はDBが履歴の正本となり、必要分はDBからページングして取得する。
TERMINAL_CACHE_GRACE_SECONDS = 60.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class GenerationJob:
    project_id: str
    panel_id: str
    candidate_count: int
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: JobStatus = "queued"
    progress: int = 0
    current: int = 0
    total: int = 0
    node: str | None = None
    message: str = "生成待ちです"
    candidate_ids: list[str] = field(default_factory=list)
    # ComfyUIのprompt_id（リモート生成中のみ）。キャンセル時のリモート停止に使う。
    prompt_id: str | None = None
    # ジョブ開始時のproject世代。構成全置換後の古い候補混入を防ぐ。
    epoch: int = 0
    # trueなら基準seedを毎回ランダム化する（同じ画像の再現を避ける）。実行時のみの
    # 指定で、コマのgeneration.seed（再現用の基準値）は書き換えない。
    randomize_seed: bool = False
    generation_input_hash: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    revision: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "panel_id": self.panel_id,
            "status": self.status,
            "progress": self.progress,
            "current": self.current,
            "total": self.total,
            "node": self.node,
            "message": self.message,
            "candidate_ids": self.candidate_ids,
            "generation_input_hash": self.generation_input_hash,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class JobManager:
    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self.jobs: dict[str, GenerationJob] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.generation_lock = asyncio.Lock()
        self.session_factory = session_factory
        self.shutting_down = False

    def create(
        self, project_id: str, panel_id: str, candidate_count: int, epoch: int = 0
    ) -> GenerationJob:
        job = GenerationJob(
            project_id=project_id,
            panel_id=panel_id,
            candidate_count=candidate_count,
            epoch=epoch,
        )
        self.jobs[job.id] = job
        self.events[job.id] = asyncio.Event()
        self.persist(job)
        return job

    def register_in_memory(self, job: GenerationJob) -> None:
        """既に同一トランザクションでGenerationJobRecordを永続化したジョブを、
        メモリキャッシュへ登録する（persistはしない）。enqueueの単一トランザクション用。"""
        self.jobs[job.id] = job
        self.events[job.id] = asyncio.Event()

    def restore_pending(self) -> tuple[list[GenerationJob], list[GenerationJob]]:
        """(再開するqueuedジョブ, 中断扱いにしたrunningジョブ) を返す。

        正常shutdownでrunningは既にerror化されるが、クラッシュ時はrunningのまま
        残るため、ここでも保険としてrunning→errorにする。中断ジョブは対応panelの
        generation.statusもerrorへ同期する必要があるため、呼び出し側へ返す。
        """
        if self.session_factory is None:
            return [], []
        with self.session_factory() as session:
            records = (
                session.query(GenerationJobRecord)
                .filter(GenerationJobRecord.status.in_(["queued", "running"]))
                .order_by(GenerationJobRecord.created_at)
                .all()
            )
            to_start: list[GenerationJob] = []
            interrupted: list[GenerationJob] = []
            for record in records:
                job = self.from_record(record)
                if record.status == "running":
                    # 再起動前にComfyUIへ投入済みの可能性があり、再キューすると二重生成になる。
                    # MVPではerror(要再実行)にし、ユーザー判断で再実行させる。
                    job.status = "error"
                    job.progress = 0
                    job.current = 0
                    job.total = 0
                    job.node = None
                    job.prompt_id = None
                    job.message = (
                        "バックエンド再起動により中断されました。必要なら再実行してください"
                    )
                    job.updated_at = utc_now()
                    self.persist(job)
                    interrupted.append(job)
                    continue
                # まだ開始していないqueuedジョブだけ安全に再開できる。
                job.status = "queued"
                job.progress = 0
                job.current = 0
                job.total = 0
                job.node = None
                job.message = "バックエンド再起動後に生成を再開します"
                job.updated_at = utc_now()
                self.jobs[job.id] = job
                self.events[job.id] = asyncio.Event()
                self.persist(job)
                to_start.append(job)
            return to_start, interrupted

    def get(self, job_id: str) -> GenerationJob | None:
        job = self.jobs.get(job_id)
        if job or self.session_factory is None:
            return job
        with self.session_factory() as session:
            record = session.get(GenerationJobRecord, job_id)
            if record is None:
                return None
            job = self.from_record(record)
            # 終了済みジョブはキャッシュへ載せない（DBが正本・メモリ肥大を防ぐ）。
            if job.status not in TERMINAL_JOB_STATUSES:
                self.jobs[job.id] = job
                self.events[job.id] = asyncio.Event()
            return job

    def list_for_project(self, project_id: str) -> list[GenerationJob]:
        if self.session_factory is None:
            return [job for job in self.jobs.values() if job.project_id == project_id]
        with self.session_factory() as session:
            records = (
                session.query(GenerationJobRecord)
                .filter(GenerationJobRecord.project_id == project_id)
                .order_by(GenerationJobRecord.created_at.desc())
                .limit(100)
                .all()
            )
            return [self.from_record(record) for record in records]

    @staticmethod
    def from_record(record: GenerationJobRecord) -> GenerationJob:
        return GenerationJob(
            id=record.id,
            project_id=record.project_id,
            panel_id=record.panel_id,
            candidate_count=record.candidate_count,
            status=cast(JobStatus, record.status),
            progress=record.progress,
            current=record.current,
            total=record.total,
            node=record.node,
            message=record.message,
            candidate_ids=json.loads(record.candidate_ids_json or "[]"),
            prompt_id=record.prompt_id,
            epoch=record.epoch,
            generation_input_hash=record.generation_input_hash,
            created_at=record.created_at.replace(tzinfo=timezone.utc)
            if record.created_at.tzinfo is None
            else record.created_at,
            updated_at=record.updated_at.replace(tzinfo=timezone.utc)
            if record.updated_at.tzinfo is None
            else record.updated_at,
        )

    def start(self, job: GenerationJob, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks[job.id] = task
        job_id = job.id
        # 完了・失敗・キャンセル後にTask参照を確実に解放する。
        task.add_done_callback(lambda _task: self._on_task_done(job_id))

    def _on_task_done(self, job_id: str) -> None:
        self.tasks.pop(job_id, None)
        if self.shutting_down:
            return
        job = self.jobs.get(job_id)
        if job is None or job.status not in TERMINAL_JOB_STATUSES:
            return
        # 終端ジョブは猶予後にメモリキャッシュ(jobs/events)から除外する。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._evict(job_id)
            return
        loop.call_later(TERMINAL_CACHE_GRACE_SECONDS, self._evict, job_id)

    def _evict(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.status not in TERMINAL_JOB_STATUSES:
            return
        self.jobs.pop(job_id, None)
        self.events.pop(job_id, None)

    def update(self, job: GenerationJob, **changes) -> None:
        next_status = changes.get("status")
        # terminal状態は単調に保つ。遅延したbackend応答・例外でcancelledをerrorへ、
        # doneをcancelledへ上書きさせない。同じterminal状態のmessage更新は許可する。
        if (
            job.status in TERMINAL_JOB_STATUSES
            and next_status is not None
            and next_status != job.status
        ):
            return
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        job.revision += 1
        self.events[job.id].set()
        self.persist(job)

    def persist(self, job: GenerationJob) -> None:
        if self.session_factory is None:
            return
        with self.session_factory() as session:
            record = session.get(GenerationJobRecord, job.id)
            if record is None:
                record = GenerationJobRecord(
                    id=job.id,
                    project_id=job.project_id,
                    panel_id=job.panel_id,
                    candidate_count=job.candidate_count,
                    epoch=job.epoch,
                    generation_input_hash=job.generation_input_hash,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                )
                session.add(record)
            record.status = job.status
            record.progress = job.progress
            record.current = job.current
            record.total = job.total
            record.node = job.node
            record.message = job.message
            record.candidate_ids_json = json.dumps(job.candidate_ids)
            record.prompt_id = job.prompt_id
            record.generation_input_hash = job.generation_input_hash
            record.updated_at = job.updated_at
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                # 親(projects)が削除済みのときだけ、cascadeでjob recordも消えており
                # 永続化対象が無いためno-opにする。親が残っているIntegrityErrorは
                # 一意/check制約違反など本物の不整合なので、隠さずログして再送出する。
                if self._project_exists(job.project_id):
                    logger.error(
                        "ジョブ永続化でIntegrityErrorが発生しました job_id=%s project_id=%s",
                        job.id,
                        job.project_id,
                    )
                    raise

    def _project_exists(self, project_id: str) -> bool:
        if self.session_factory is None:
            return False
        with self.session_factory() as session:
            return session.get(ProjectRecord, project_id) is not None

    async def wait_for_change(
        self, job_id: str, revision: int, timeout: float = 15.0
    ) -> GenerationJob:
        job = self.jobs[job_id]
        if job.revision != revision:
            return job
        event = self.events[job_id]
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return job

    def cancel(self, job: GenerationJob) -> bool:
        """queued/runningからのみcancelledへ遷移する。状態遷移の唯一の窓口。

        遷移したらTrue、既にdone/error/cancelledなら何もせずFalseを返す。
        完了直後のキャンセルで成功済みコマをskipped扱いにしないため、呼び出し側は
        Trueのときだけリモート停止やパネル状態更新を行うこと。
        """
        if job.status in TERMINAL_JOB_STATUSES:
            return False
        task = self.tasks.get(job.id)
        if task:
            task.cancel()
        self.update(job, status="cancelled", message="生成をキャンセルしました")
        return True

    async def shutdown(self) -> None:
        self.shutting_down = True
        tasks = [task for task in self.tasks.values() if not task.done()]
        for job_id, task in self.tasks.items():
            if task.done():
                continue
            job = self.jobs.get(job_id)
            if job is None:
                continue
            if job.status == "running":
                # 既にComfyUIへ投入済みかもしれない。queuedで残すと次回起動で再投入され
                # 二重生成になるため、errorにしてprompt_idもクリアする。
                # queuedのままのジョブは状態を変えず、restore_pendingが安全に再開する。
                self.update(
                    job,
                    status="error",
                    progress=0,
                    node=None,
                    prompt_id=None,
                    message="バックエンド停止により中断されました。必要なら再実行してください",
                )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
