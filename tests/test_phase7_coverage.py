from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

import backend.app.asset_storage as asset_storage_module
import backend.app.generation_service as generation_module
from backend.app.config import Settings
from backend.app.database import ProjectRecord, ProjectRevisionRecord, now_utc
from backend.app.jobs import JobManager
from backend.app.main import create_app
from backend.app.mutation import (
    InvalidProjectJsonError,
    PanelNotFoundError,
    ProjectConflictError,
    ProjectNotFoundError,
)
from backend.app.schemas import MangaProject


def make_client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        export_dir=tmp_path / "exports",
        knowledge_dir=tmp_path / "knowledge",
        image_backend="stub",
        llm_provider="stub",
    )
    from fastapi.testclient import TestClient

    return TestClient(create_app(settings))


def create_project(client) -> str:
    project_id = client.post(
        "/api/projects", json={"title": "本", "work_name": "作品", "target_pages": 4}
    ).json()["project"]["id"]
    client.post(
        f"/api/projects/{project_id}/generate-name?revision=0",
        json={
            "work_name": "作品",
            "character_a": "春香",
            "character_b": "千早",
            "situation": "事務所で相談する",
            "ending_direction": "笑って終わる",
        },
    )
    return project_id


def revision(client, project_id: str) -> int:
    return client.get(f"/api/projects/{project_id}").json()["revision"]


def png_bytes(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(buffer, format="PNG")
    return buffer.getvalue()


# --- iter_manga_asset_strings: 全asset種別の列挙 ---


def test_iter_manga_asset_strings_collects_every_asset_field() -> None:
    manga = MangaProject.model_validate(
        {
            "title": "assets",
            "target_pages": 4,
            "characters": [
                {"id": "c1", "display_name": "A", "reference_image_asset": "exports/p/char.png"}
            ],
            "locations": [
                {"id": "l1", "display_name": "L", "reference_image_asset": "exports/p/loc.png"}
            ],
            "pages": [
                {
                    "page": 1,
                    "theme": "t",
                    "layout_template": "x",
                    "render_asset": "exports/p/page1.png",
                    "overlay_elements": [
                        {
                            "id": "ov1",
                            "source_panel_id": "p01_01",
                            "box": [0.1, 0.1, 0.3, 0.3],
                            "asset": "exports/p/ov.png",
                            "mask_asset": "exports/p/ov-mask.png",
                        }
                    ],
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0, 0, 1, 1],
                            "shot": "s",
                            "image_asset": "exports/p/panel.png",
                            "control_references": [
                                {
                                    "id": "ctrl1",
                                    "kind": "pose",
                                    "asset": "exports/p/pose.png",
                                    "load_node_id": "50",
                                }
                            ],
                            "generation": {
                                "reference_images": [
                                    {"node_id": "30", "asset": "exports/p/ref.png"}
                                ]
                            },
                            "image_candidates": [
                                {
                                    "id": "cand1",
                                    "asset": "exports/p/cand.png",
                                    "backend": "stub",
                                    "status": "done",
                                    "seed": 1,
                                    "created_at": "2024-01-01T00:00:00+00:00",
                                    "reference_images": [
                                        {"node_id": "31", "asset": "exports/p/cand-ref.png"}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    collected = set(asset_storage_module.iter_manga_asset_strings(manga))
    assert collected == {
        "exports/p/char.png",
        "exports/p/loc.png",
        "exports/p/page1.png",
        "exports/p/ov.png",
        "exports/p/ov-mask.png",
        "exports/p/panel.png",
        "exports/p/pose.png",
        "exports/p/ref.png",
        "exports/p/cand.png",
        "exports/p/cand-ref.png",
    }


def test_generation_input_hash_digests_present_and_missing_assets(tmp_path: Path) -> None:
    """generation_input_hashが参照画像の内容hash（存在/欠損）を取り込むこと。"""
    export_dir = tmp_path / "exports"
    (export_dir / "proj").mkdir(parents=True)
    Image.new("RGB", (4, 4), "red").save(export_dir / "proj" / "present.png")
    manga = MangaProject.model_validate(
        {
            "title": "digest",
            "target_pages": 4,
            "pages": [
                {
                    "page": 1,
                    "theme": "t",
                    "layout_template": "x",
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0, 0, 1, 1],
                            "shot": "s",
                            "control_references": [
                                {
                                    "id": "c1",
                                    "kind": "pose",
                                    "asset": "proj/present.png",
                                    "load_node_id": "50",
                                }
                            ],
                            "generation": {
                                "reference_images": [{"node_id": "31", "asset": "proj/missing.png"}]
                            },
                        }
                    ],
                }
            ],
        }
    )
    panel = manga.pages[0].panels[0]
    digest = generation_module.generation_input_hash(manga, panel, export_dir)
    assert isinstance(digest, str) and len(digest) == 64
    # 存在assetの内容が変わるとhashも変わる。
    Image.new("RGB", (4, 4), "blue").save(export_dir / "proj" / "present.png")
    assert generation_module.generation_input_hash(manga, panel, export_dir) != digest


def test_referenced_paths_skip_malformed_revision_json(tmp_path: Path) -> None:
    """referenced_project_asset_pathsがリビジョン履歴も走査し、破損JSONは無視すること。"""
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_project(client)
        with app.state.SessionLocal() as session:
            session.add(
                ProjectRevisionRecord(
                    id="rev-broken",
                    project_id=project_id,
                    label="broken",
                    manga_json="{壊れたJSON",
                    created_at=now_utc(),
                )
            )
            session.commit()
        paths = app.state.rendering.referenced_project_asset_paths(project_id)
        assert isinstance(paths, set)


# --- 参照画像アップロードの楽観ロック競合（cleanup except branch） ---


def add_overlay(client, project_id: str) -> None:
    detail = client.get(f"/api/projects/{project_id}").json()
    manga = detail["manga_json"]
    manga["pages"][0]["overlay_elements"] = [
        {
            "id": "ov1",
            "source_panel_id": manga["pages"][0]["panels"][0]["panel_id"],
            "box": [0.2, 0.2, 0.4, 0.4],
        }
    ]
    assert (
        client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        ).status_code
        == 200
    )


def bump_revision(client, project_id: str) -> None:
    detail = client.get(f"/api/projects/{project_id}").json()
    assert (
        client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}",
            json=detail["manga_json"],
        ).status_code
        == 200
    )


def test_stale_revision_uploads_conflict_and_cleanup(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        add_overlay(client, project_id)
        stale = revision(client, project_id)
        bump_revision(client, project_id)  # revisionを進めてstaleを陳腐化させる

        # character / location / control / overlay すべて、保存後にrun_mutationが409となり
        # cleanup（except branch）を通る。
        endpoints = [
            f"/api/projects/{project_id}/characters/char_a/reference-image?revision={stale}",
            f"/api/projects/{project_id}/locations/default_room/reference-image?revision={stale}",
            f"/api/projects/{project_id}/panels/p01_01/controls/pose/reference-image?load_node_id=51&revision={stale}",
            f"/api/projects/{project_id}/pages/1/overlays/ov1/asset?revision={stale}",
        ]
        for path in endpoints:
            response = client.post(
                path, content=png_bytes("red"), headers={"Content-Type": "image/png"}
            )
            assert response.status_code == 409, path
        # 孤児.tmpが残らないこと。
        assert list((tmp_path / "exports").rglob("*.tmp")) == []


def test_upload_missing_entities_return_404(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        rev = revision(client, project_id)
        cases = [
            f"/api/projects/{project_id}/characters/none/reference-image?revision={rev}",
            f"/api/projects/{project_id}/locations/none/reference-image?revision={rev}",
            f"/api/projects/{project_id}/panels/none/controls/pose/reference-image?load_node_id=1&revision={rev}",
            f"/api/projects/{project_id}/pages/1/overlays/none/asset?revision={rev}",
            f"/api/projects/{project_id}/pages/999/overlays/x/asset?revision={rev}",
        ]
        for path in cases:
            response = client.post(
                path, content=png_bytes("blue"), headers={"Content-Type": "image/png"}
            )
            assert response.status_code == 404, path


def test_stale_revision_precedes_missing_entity_errors(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        stale = revision(client, project_id)
        bump_revision(client, project_id)
        cases = [
            f"/api/projects/{project_id}/characters/none/reference-image?revision={stale}",
            f"/api/projects/{project_id}/locations/none/reference-image?revision={stale}",
            f"/api/projects/{project_id}/panels/none/controls/pose/reference-image?load_node_id=1&revision={stale}",
            f"/api/projects/{project_id}/pages/1/overlays/none/asset?revision={stale}",
        ]
        for path in cases:
            response = client.post(
                path, content=png_bytes("blue"), headers={"Content-Type": "image/png"}
            )
            assert response.status_code == 409, path
            assert response.json()["code"] == "project_revision_conflict"


# --- mutation service のdomain例外 ---


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


# --- GenerationService.enqueue のdomain例外 ---


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


# --- JobManager の補助分岐 ---


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
    from backend.app.database import create_session_factory, now_utc

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


# --- schemas: 保存不能な構造破綻はValidationError ---


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["pages"].append({**m["pages"][0]}),  # ページ番号重複
        lambda m: m["pages"][0]["panels"].append({**m["pages"][0]["panels"][0]}),  # コマID重複
        lambda m: m["workflow_presets"].append({**m["workflow_presets"][0]}),  # preset ID重複
        lambda m: m.update({"active_workflow_preset_id": "ghost"}),  # 既定preset参照切れ
        lambda m: m["pages"][0]["panels"][0]["generation"].update(
            {"workflow_preset_id": "ghost"}
        ),  # コマのpreset参照切れ
    ],
)
def test_manga_consistency_rejects_structural_breakage(tmp_path: Path, mutate) -> None:
    with make_client(tmp_path) as client:
        project_id = create_project(client)
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        mutate(manga)
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert response.status_code == 422
