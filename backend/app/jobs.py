from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


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
    def __init__(self) -> None:
        self.jobs: dict[str, GenerationJob] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.events: dict[str, asyncio.Event] = {}

    def create(self, project_id: str, panel_id: str, candidate_count: int) -> GenerationJob:
        job = GenerationJob(project_id=project_id, panel_id=panel_id, candidate_count=candidate_count)
        self.jobs[job.id] = job
        self.events[job.id] = asyncio.Event()
        return job

    def get(self, job_id: str) -> GenerationJob | None:
        return self.jobs.get(job_id)

    def start(self, job: GenerationJob, coroutine) -> None:
        self.tasks[job.id] = asyncio.create_task(coroutine)

    def update(self, job: GenerationJob, **changes) -> None:
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        job.revision += 1
        self.events[job.id].set()

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
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
