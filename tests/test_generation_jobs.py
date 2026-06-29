"""生成ジョブと並行制御のテスト。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import (
    create_stub_project as create_project,
)
from conftest import (
    make_stub_client as make_client,
)
from conftest import (
    mutation_url,
)

import backend.app.generation_service as generation_module
from backend.app import database, story
from backend.app.config import Settings
from backend.app.database import (
    ProjectRecord,
    create_session_factory,
    now_utc,
)
from backend.app.database import now_utc as db_now_utc
from backend.app.jobs import JobManager
from backend.app.mutation import (
    PanelNotFoundError,
    ProjectNotFoundError,
)

VALID_BRIEF = json.dumps(
    {
        "synopsis": "海辺の町の短い物語",
        "tone": "穏やか",
        "characters": [{"name": "海斗", "role": "主役"}],
        "canon_conditions": ["原作の地名を守る"],
    },
    ensure_ascii=False,
)


def test_same_session_generation_rejects_concurrent_request(tmp_path: Path) -> None:
    class SlowLLM:
        provider = "openai_compatible"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(self, messages: list[dict], want_json: bool = True, on_progress=None) -> str:
            if on_progress is not None:
                on_progress("途中")
            self.started.set()
            await self.release.wait()
            return VALID_BRIEF

    async def run_case() -> None:
        factory = create_session_factory(f"sqlite:///{tmp_path / 'concurrent-story.db'}")
        settings = Settings()
        with factory() as setup:
            setup.add(
                ProjectRecord(
                    id="p1",
                    title="t",
                    work_name="作品",
                    manga_json="{}",
                    created_at=db_now_utc(),
                    updated_at=db_now_utc(),
                )
            )
            setup.commit()
            record = story.create_session(
                setup, project_id="p1", work_name="作品", target_pages=4, instruction="日常"
            )
            session_id = record.id

        llm = SlowLLM()
        with factory() as first_session, factory() as second_session:
            first_record = first_session.get(database.StoryGenerationSessionRecord, session_id)
            second_record = second_session.get(database.StoryGenerationSessionRecord, session_id)
            task = asyncio.create_task(
                story.generate_stage(first_session, llm, settings, first_record, "brief")
            )
            await llm.started.wait()
            assert story.get_generation_progress(session_id)["chars"] == 2
            try:
                await story.generate_stage(second_session, llm, settings, second_record, "brief")
                assert False, "同一sessionの並行生成は拒否すること"
            except story.StoryError as exc:
                assert exc.status_code == 409
            assert story.get_generation_progress(session_id)["chars"] == 2
            llm.release.set()
            await task
        assert story.get_generation_progress(session_id) is None

    asyncio.run(run_case())


def test_generation_progress_idle_when_not_generating(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        session = client.post(
            mutation_url(client, project_id, "story-sessions"), json={"target_pages": 4}
        ).json()
        session_id = session["result"]["id"]
        progress = client.get(f"/api/story-sessions/{session_id}/generation-progress").json()
        assert progress["phase"] == "idle"
        assert progress["chars"] == 0


def test_generation_service_enqueue_error_paths(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_project(client)

        with pytest.raises(ProjectNotFoundError):
            app.state.generation.enqueue("missing", ["p01_01"], 1, "m")

        with pytest.raises(PanelNotFoundError):
            app.state.generation.enqueue(project_id, ["nope"], 1, "m")

        # enqueueのepoch不一致は、登録文脈の409文言を維持するためScope競合へ変換する。
        with pytest.raises(generation_module.JobEnqueueScopeConflictError):
            app.state.generation.enqueue(project_id, ["p01_01"], 1, "m", expected_epoch=99999)

        # 既にqueuedのコマへ再登録するとActiveJobConflict→409。
        app.state.generation.enqueue(project_id, ["p01_01"], 1, "m")
        with pytest.raises(generation_module.ActiveJobConflictError):
            app.state.generation.enqueue(project_id, ["p01_01"], 1, "m")

        # skip_active=Trueで全コマがactive除外され空になる場合もActiveJobConflict→409。
        with pytest.raises(generation_module.ActiveJobConflictError):
            app.state.generation.enqueue(project_id, ["p01_01"], 1, "m", skip_active=True)


def test_job_manager_without_session_factory() -> None:
    manager = JobManager()
    assert manager.restore_pending() == ([], [])
    assert manager.get("unknown") is None
    job = manager.create("project", "panel", 1)
    # terminal到達後はstatus退行させない。
    manager.update(job, status="done")
    manager.update(job, status="cancelled")
    assert job.status == "done"
    # 完了済みjobのcancelはFalse。
    assert manager.cancel(job) is False


def test_job_manager_loads_job_from_db(tmp_path: Path) -> None:
    from backend.app.database import create_session_factory

    factory = create_session_factory(f"sqlite:///{tmp_path / 'jobs.db'}")
    # generation_jobsはprojectsへFKを持つため、先にプロジェクトを用意する。
    with factory() as session:
        session.add(
            ProjectRecord(
                id="proj",
                title="t",
                work_name="",
                manga_json="{}",
                created_at=now_utc(),
                updated_at=now_utc(),
            )
        )
        session.commit()
    writer = JobManager(factory)
    job = writer.create("proj", "panel", 1)

    # 別マネージャはDBから読み込み、非terminalはキャッシュへ載せる。
    reader = JobManager(factory)
    loaded = reader.get(job.id)
    assert loaded is not None and loaded.id == job.id
    assert job.id in reader.jobs

    # terminalなジョブはDBから読めるがキャッシュへ載せない。
    writer.update(job, status="done")
    fresh = JobManager(factory)
    done = fresh.get(job.id)
    assert done is not None and done.status == "done"
    assert job.id not in fresh.jobs
