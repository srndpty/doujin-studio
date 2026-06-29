"""mutationとrevision競合のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    create_stub_project as create_project,
)
from conftest import (
    latest_revision as revision,
)
from conftest import (
    make_stub_client as make_client,
)

from backend.app.database import (
    ProjectRecord,
    StoryGenerationSessionRecord,
    now_utc,
)
from backend.app.mutation import (
    InvalidProjectJsonError,
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectRevisionConflictError,
)
from backend.app.schemas import (
    MangaProject,
)


def test_mutation_service_error_paths(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        service = app.state.mutation
        project_id = create_project(client)
        manga = MangaProject.model_validate(
            client.get(f"/api/projects/{project_id}").json()["manga_json"]
        )

        def noop(_manga: MangaProject) -> None:
            return None

        with pytest.raises(ProjectNotFoundError):
            service.mutate_local("missing", noop)

        with pytest.raises(ProjectConflictError):
            service.mutate_user(project_id, expected_revision=99999, mutate=noop)

        with pytest.raises(ProjectNotFoundError):
            service.replace("missing", manga, expected_revision=0)
        with pytest.raises(ProjectConflictError):
            service.replace(project_id, manga, expected_revision=99999)

        with pytest.raises(ProjectNotFoundError):
            service.replace_with_history(
                "missing",
                lambda _session, latest: latest,
                expected_revision=0,
                history_label="x",
            )
        with pytest.raises(ProjectConflictError):
            service.replace_with_history(
                project_id,
                lambda _session, latest: latest,
                expected_revision=99999,
                history_label="x",
            )

        # 破損Manga JSONはInvalidProjectJsonErrorを送出する。
        with app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, project_id)
            record.manga_json = "{壊れたJSON"
            session.commit()
        with pytest.raises(InvalidProjectJsonError):
            service.mutate_local(project_id, noop)

        with pytest.raises(ProjectNotFoundError):
            service.current_epoch("missing")


def test_mutate_user_transaction_noop_path_cas_guards_revision(tmp_path: Path) -> None:
    """manga_jsonを変えないstory session作成でも、確認後の別更新を競合として弾く。

    revision確認 → 関連レコードのflush → commit の間に別操作がrevisionを進めても、
    no-op経路はCASでrevision一致を保証し、競合をすり抜けてcommitしない。
    """
    with make_client(tmp_path) as client:
        app = client.app
        service = app.state.mutation
        project_id = create_project(client)
        base = revision(client, project_id)

        def mutate(session, manga: MangaProject) -> str:
            # manga_jsonは一切変えず（no-op経路）、関連レコードだけ追加する。
            session.add(
                StoryGenerationSessionRecord(
                    id="story-noop",
                    project_id=project_id,
                    work_name="作品",
                    target_pages=4,
                    instruction="",
                    stages_json="{}",
                    created_at=now_utc(),
                    updated_at=now_utc(),
                )
            )
            # 確認後・commit前に別操作がrevisionを進める。
            service.mutate_local(project_id, lambda latest: setattr(latest, "premise", "別更新"))
            return "story-noop"

        with pytest.raises(ProjectRevisionConflictError):
            service.mutate_user_transaction(project_id, expected_revision=base, mutate=mutate)

        # rollbackされ、story sessionは作成されていない。
        with app.state.SessionLocal() as session:
            assert session.get(StoryGenerationSessionRecord, "story-noop") is None


def test_create_story_session_conflict_returns_409_without_session(tmp_path: Path) -> None:
    """project不変のstory session作成APIも、stale revisionでは409かつsession未作成。"""
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        stale = revision(client, project_id)
        # 別操作でrevisionを進めてからstale revisionで作成を試みる。
        client.post(
            f"/api/projects/{project_id}/generate-name?revision={stale}",
            json={
                "work_name": "作品",
                "character_a": "A",
                "character_b": "B",
                "situation": "x",
                "ending_direction": "y",
            },
        )
        response = client.post(
            f"/api/projects/{project_id}/story-sessions?revision={stale}",
            json={"target_pages": 4},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "project_revision_conflict"
        sessions = client.get(f"/api/projects/{project_id}/story-sessions").json()
        assert sessions == []
