from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import sessionmaker

from .database import GenerationJobRecord


JobStatus = Literal["queued", "running", "done", "error", "cancelled"]
TERMINAL_JOB_STATUSES = {"done", "error", "cancelled"}


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

    def create(self, project_id: str, panel_id: str, candidate_count: int) -> GenerationJob:
        job = GenerationJob(project_id=project_id, panel_id=panel_id, candidate_count=candidate_count)
        self.jobs[job.id] = job
        self.events[job.id] = asyncio.Event()
        self.persist(job)
        return job

    def restore_pending(self) -> list[GenerationJob]:
        if self.session_factory is None:
            return []
        with self.session_factory() as session:
            records = (
                session.query(GenerationJobRecord)
                .filter(GenerationJobRecord.status.in_(["queued", "running"]))
                .order_by(GenerationJobRecord.created_at)
                .all()
            )
            jobs: list[GenerationJob] = []
            for record in records:
                job = self.from_record(record)
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
                jobs.append(job)
            return jobs

    def get(self, job_id: str) -> GenerationJob | None:
        job = self.jobs.get(job_id)
        if job or self.session_factory is None:
            return job
        with self.session_factory() as session:
            record = session.get(GenerationJobRecord, job_id)
            if record is None:
                return None
            job = self.from_record(record)
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
            status=record.status,
            progress=record.progress,
            current=record.current,
            total=record.total,
            node=record.node,
            message=record.message,
            candidate_ids=json.loads(record.candidate_ids_json or "[]"),
            created_at=record.created_at.replace(tzinfo=timezone.utc) if record.created_at.tzinfo is None else record.created_at,
            updated_at=record.updated_at.replace(tzinfo=timezone.utc) if record.updated_at.tzinfo is None else record.updated_at,
        )

    def start(self, job: GenerationJob, coroutine) -> None:
        self.tasks[job.id] = asyncio.create_task(coroutine)

    def update(self, job: GenerationJob, **changes) -> None:
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
            record.updated_at = job.updated_at
            session.commit()

    async def wait_for_change(self, job_id: str, revision: int, timeout: float = 15.0) -> GenerationJob:
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

    def cancel(self, job: GenerationJob) -> None:
        if job.status in TERMINAL_JOB_STATUSES:
            return
        task = self.tasks.get(job.id)
        if task:
            task.cancel()
        self.update(job, status="cancelled", message="生成をキャンセルしました")

    async def shutdown(self) -> None:
        self.shutting_down = True
        tasks = [task for task in self.tasks.values() if not task.done()]
        for job_id, task in self.tasks.items():
            if not task.done():
                job = self.jobs[job_id]
                self.update(job, status="queued", progress=0, message="バックエンド停止後に再開します")
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
