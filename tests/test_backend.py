from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import backend.app.generation_service as generation_module
import backend.app.project_render_service as project_render_module
import backend.app.rendering as rendering_module
import backend.app.routers.common as router_common
import backend.app.routers.projects as projects_router
from backend.app import assets as assets_module
from backend.app import database as database_module
from backend.app.config import Settings
from backend.app.database import (
    GenerationJobRecord,
    ProjectRecord,
    ProjectRevisionRecord,
    create_session_factory,
)
from backend.app.database import now_utc as db_now_utc
from backend.app.generator import DEFAULT_COMMON_NEGATIVE_PROMPT, DEFAULT_COMMON_POSITIVE_PROMPT
from backend.app.image_backends import (
    ComfyUIWorkflowConfig,
    ImageResult,
    apply_panel_to_workflow,
    apply_reference_images_to_workflow,
)
from backend.app.jobs import GenerationJob, JobManager
from backend.app.main import create_app
from backend.app.mutation import EpochMismatchError, ProjectConflictError
from backend.app.prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from backend.app.renderer import fit_image_to_box, sanitize_export_filename
from backend.app.rendering import RenderInputChangedError
from backend.app.schemas import (
    Dialogue,
    GenerationInfo,
    ImageCandidate,
    MangaProject,
    Page,
    Panel,
)


def update_panel_in_latest(app, project_id: str, panel_id: str, mutate):
    return app.state.generation.update_panel_in_latest(
        project_id,
        panel_id,
        mutate,
        expected_epoch=app.state.mutation.current_epoch(project_id),
    )


def mark_panel_job_stopped(app, job: GenerationJob, message: str, error: bool = False) -> None:
    app.state.generation.mark_panel_job_stopped(job, message, error=error)


def commit_rendered_pages(
    app, project_id: str, snapshot: MangaProject, assets: list[Path], **kwargs
):
    try:
        committed = app.state.rendering.commit_rendered_pages(
            project_id, snapshot, assets, **kwargs
        )
    except RenderInputChangedError as exc:
        raise HTTPException(status_code=409, detail="描画入力競合") from exc
    except (ProjectConflictError, EpochMismatchError) as exc:
        raise HTTPException(status_code=409, detail="描画結果の確定中に競合しました") from exc
    return committed.manga, committed.revision


def make_client(
    tmp_path: Path, image_backend: str = "stub", workflow_path: Path | None = None
) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        export_dir=tmp_path / "exports",
        image_backend=image_backend,
        comfyui_base_url="http://127.0.0.1:9",
        comfyui_workflow_path=workflow_path or tmp_path / "missing.workflow_api.json",
    )
    return TestClient(create_app(settings))


def put_manga(client: TestClient, project_id: str, manga: dict):
    # manga-jsonは楽観ロックでrevision必須。最新revisionを取得して保存する。
    revision = client.get(f"/api/projects/{project_id}").json()["revision"]
    return client.put(f"/api/projects/{project_id}/manga-json?revision={revision}", json=manga)


def current_revision(client: TestClient, project_id: str) -> int:
    return client.get(f"/api/projects/{project_id}").json()["revision"]


def mutation_url(client: TestClient, project_id: str, suffix: str) -> str:
    separator = "&" if "?" in suffix else "?"
    return (
        f"/api/projects/{project_id}/{suffix}{separator}revision="
        f"{current_revision(client, project_id)}"
    )


def create_generated_project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"title": "テスト本", "work_name": "テスト作品"})
    assert response.status_code == 200
    project_id = response.json()["project"]["id"]
    response = client.post(
        f"/api/projects/{project_id}/generate-name?revision=0",
        json={
            "work_name": "テスト作品",
            "character_a": "春香",
            "character_b": "千早",
            "situation": "事務所で差し入れを選ぶ",
            "ending_direction": "小さな勘違いで笑って終わる",
        },
    )
    assert response.status_code == 200
    return project_id


def test_project_crud_and_generate_name(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.get(f"/api/projects/{project_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["manga_json"]["target_pages"] == 4
        assert len(payload["manga_json"]["pages"]) == 4
        assert payload["manga_json"]["characters"][0]["display_name"] == "春香"
        assert payload["manga_json"]["common_positive_prompt"] == DEFAULT_COMMON_POSITIVE_PROMPT
        assert payload["manga_json"]["common_negative_prompt"] == DEFAULT_COMMON_NEGATIVE_PROMPT
        first_panel = payload["manga_json"]["pages"][0]["panels"][0]
        # 生成サイズはコマの縦横比から64px単位で算出する。
        assert first_panel["generation"]["width"] % 64 == 0
        assert first_panel["generation"]["height"] % 64 == 0
        assert first_panel["generation"]["width"] > first_panel["generation"]["height"]
        assert "establishing shot" in first_panel["generation"]["prompt"]
        assert payload["manga_json"]["characters"][0]["trigger_prompt"] == "春香"
        # 日本漫画向けの既定（右綴じ・縦書き写植）。
        assert payload["manga_json"]["reading_direction"] == "rtl"
        assert payload["manga_json"]["typography"]["default_font_size"] == 34


def test_render_and_export_cbz(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(mutation_url(client, project_id, "render"))
        assert response.status_code == 200
        assets = response.json()["result"]["page_assets"]
        assert len(assets) == 4
        for asset in assets:
            asset_response = client.get(f"/api/assets/{asset}")
            assert asset_response.status_code == 200

        response = client.post(mutation_url(client, project_id, "export/cbz"))
        assert response.status_code == 200
        export_result = response.json()["result"]
        cbz_asset = export_result["cbz_asset"]
        cbz_path = tmp_path / "exports" / cbz_asset
        assert cbz_path.exists()
        assert cbz_path.name.startswith("テスト本-")
        assert "-r" in cbz_path.name
        assert cbz_path.name.endswith(".cbz")
        assert Path(export_result["absolute_path"]) == cbz_path.resolve()
        with ZipFile(cbz_path) as archive:
            assert archive.namelist() == [
                "page_001.png",
                "page_002.png",
                "page_003.png",
                "page_004.png",
            ]
        second = client.post(mutation_url(client, project_id, "export/cbz"))
        assert second.status_code == 200
        assert second.json()["result"]["cbz_asset"] != cbz_asset
        assert export_result["warnings"] == []
        status = client.get(f"/api/projects/{project_id}/production-status").json()
        assert status["status"] == "complete"
        assert status["adopted_panels"] == status["total_panels"]


def test_open_export_folder_selects_cbz(tmp_path: Path, monkeypatch) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(projects_router, "open_in_file_manager", lambda path: opened.append(path))
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        client.post(mutation_url(client, project_id, "export/cbz"))
        response = client.post(f"/api/projects/{project_id}/export/open-folder")
        assert response.status_code == 200
        payload = response.json()
        assert payload["cbz_exists"] is True
        assert Path(payload["cbz_path"]).name.startswith("テスト本-")
        assert opened == [Path(payload["cbz_path"])]


def test_export_filename_is_safe_for_windows() -> None:
    assert sanitize_export_filename(" 本:第1話/「始まり」? ") == "本_第1話_「始まり」_"
    assert sanitize_export_filename(" . ") == "名称未設定"


def test_comfyui_unavailable_falls_back_to_stub(tmp_path: Path) -> None:
    # ワークフローは正しいが接続不可（一時障害）のときはstubへ退避する。
    workflow_path = tmp_path / "workflow_api.json"
    workflow_path.write_text(json.dumps(sample_workflow()), encoding="utf-8")
    with make_client(tmp_path, image_backend="comfyui", workflow_path=workflow_path) as client:
        project_id = create_generated_project(client)
        response = client.post(mutation_url(client, project_id, "render"))
        assert response.status_code == 200
        manga = response.json()["project"]["manga_json"]
        first_panel = manga["pages"][0]["panels"][0]
        assert first_panel["generation"]["backend"] == "comfyui"
        assert first_panel["generation"]["status"] == "fallback"
        assert first_panel["image_asset"]


def test_comfyui_missing_workflow_is_error_not_silent_stub(tmp_path: Path) -> None:
    # 設定不備（ワークフロー欠如）は黙ってstubへ退避せず、エラーとして表面化させる。
    with make_client(tmp_path, image_backend="comfyui") as client:
        project_id = create_generated_project(client)
        response = client.post(mutation_url(client, project_id, "render"))
        assert response.status_code == 502
        project = client.get(f"/api/projects/{project_id}").json()
        first_panel = project["manga_json"]["pages"][0]["panels"][0]
        assert first_panel["generation"]["status"] == "error"
        assert first_panel["image_asset"] is None
        assert all(page["render_status"] == "pending" for page in project["manga_json"]["pages"])
        assert not list((tmp_path / "exports" / project_id / "pages").glob("*.png"))


def test_generate_image_returns_502_when_backend_fails(tmp_path: Path) -> None:
    with make_client(tmp_path, image_backend="comfyui") as client:
        project_id = create_generated_project(client)
        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        assert response.status_code == 502
        panel = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
            "panels"
        ][0]
        assert panel["generation"]["status"] == "error"


def test_invalid_manga_json_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/projects", json={"title": "不正テスト", "work_name": ""})
        project_id = response.json()["project"]["id"]
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision=0",
            json={
                "title": "不正",
                "target_pages": 4,
                "pages": [{"page": 1, "theme": "x", "layout_template": "x", "panels": []}],
            },
        )
        assert response.status_code == 422


def test_manga_json_optimistic_lock_rejects_stale_revision(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        detail = client.get(f"/api/projects/{project_id}").json()
        revision = detail["revision"]
        manga = detail["manga_json"]

        first = client.put(f"/api/projects/{project_id}/manga-json?revision={revision}", json=manga)
        assert first.status_code == 200
        assert first.json()["project"]["revision"] == revision + 1

        # 古いrevisionでの保存は409で弾く（生成完了・別タブ保存との競合）。
        stale = client.put(f"/api/projects/{project_id}/manga-json?revision={revision}", json=manga)
        assert stale.status_code == 409
        conflict = stale.json()
        assert conflict["code"] == "project_revision_conflict"
        assert conflict["expected_revision"] == revision
        assert conflict["actual_revision"] == revision + 1
        assert conflict["project"]["revision"] == revision + 1

        # revisionは必須。未指定の無条件保存は許可しない（古いJSONでの巻き戻し防止）。
        missing = client.put(f"/api/projects/{project_id}/manga-json", json=manga)
        assert missing.status_code == 422


def test_generate_name_rejects_stale_revision(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        stale = client.post(
            f"/api/projects/{project_id}/generate-name?revision=0",
            json={
                "work_name": "別作品",
                "character_a": "A",
                "character_b": "B",
                "situation": "別の場面",
                "ending_direction": "別の結末",
            },
        )
        assert stale.status_code == 409
        assert client.get(f"/api/projects/{project_id}").json()["work_name"] == "テスト作品"


def test_generation_merge_preserves_concurrent_panel_edit(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app

        # ユーザーがp01_02のプロンプトを編集して保存する。
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        for panel in manga["pages"][0]["panels"]:
            if panel["panel_id"] == "p01_02":
                panel["prompt"] = "ユーザー編集後のプロンプト"
        assert put_manga(client, project_id, manga).status_code == 200

        # 生成完了が開始時点の古いスナップショットではなく最新へp01_01だけマージする。
        update_panel_in_latest(
            app,
            project_id,
            "p01_01",
            lambda panel, page: setattr(panel.generation, "message", "マージ済み"),
        )

        updated = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        panels = {panel["panel_id"]: panel for panel in updated["pages"][0]["panels"]}
        # p01_01のマージが反映され、かつp01_02のユーザー編集が消えていない。
        assert panels["p01_01"]["generation"]["message"] == "マージ済み"
        assert panels["p01_02"]["prompt"] == "ユーザー編集後のプロンプト"


def test_duplicate_panel_id_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        panels = manga["pages"][0]["panels"]
        panels[1]["panel_id"] = panels[0]["panel_id"]
        response = put_manga(client, project_id, manga)
        assert response.status_code == 422


def test_metadata_change_keeps_rendered_pages_but_render_change_invalidates(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        assert client.post(mutation_url(client, project_id, "render")).status_code == 200
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        assert all(page["render_status"] == "done" for page in manga["pages"])

        # メタ変更（タイトル）だけでは全ページをpendingに戻さない。
        manga["title"] = "新しいタイトル"
        meta_only = put_manga(client, project_id, manga)
        assert meta_only.status_code == 200
        assert all(
            page["render_status"] == "done"
            for page in meta_only.json()["project"]["manga_json"]["pages"]
        )

        # 描画に影響する変更（1ページ目の台詞追加）は当該ページだけpendingにする。
        manga2 = meta_only.json()["project"]["manga_json"]
        manga2["pages"][0]["panels"][0]["dialogue"].append(
            {"speaker": "char_a", "text": "追加の台詞"}
        )
        render_change = put_manga(client, project_id, manga2)
        assert render_change.status_code == 200
        pages = render_change.json()["project"]["manga_json"]["pages"]
        assert pages[0]["render_status"] == "pending"
        assert all(page["render_status"] == "done" for page in pages[1:])


def test_duplicate_workflow_preset_id_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        preset = manga["workflow_presets"][0]
        manga["workflow_presets"] = [preset, {**preset, "name": "重複ID"}]
        response = put_manga(client, project_id, manga)
        assert response.status_code == 422


def test_generation_keeps_user_selection_made_during_run(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)
        # 既存候補を2つ作る（2回目が選択状態になる）。
        client.post(mutation_url(client, project_id, "panels/p01_01/use-stub"))
        client.post(mutation_url(client, project_id, "panels/p01_01/use-stub"))
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        panel = next(p for p in manga["pages"][0]["panels"] if p["panel_id"] == "p01_01")
        assert len(panel["image_candidates"]) == 2
        user_pick = panel["image_candidates"][0]["id"]

        class SelectingBackend:
            def __init__(self) -> None:
                self.done = False

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                # 生成中にユーザーが別の既存候補を選び直した状況を再現する。
                if not self.done:
                    self.done = True
                    update_panel_in_latest(
                        app,
                        project_id_,
                        "p01_01",
                        lambda target_panel, target_page: setattr(
                            target_panel, "selected_candidate_id", user_pick
                        ),
                    )
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (9, 9, 9)).save(target_path)
                return ImageResult("stub", "done", target_path, "ok")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: SelectingBackend()
        )

        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        assert response.status_code == 200
        panel2 = next(
            p
            for p in response.json()["project"]["manga_json"]["pages"][0]["panels"]
            if p["panel_id"] == "p01_01"
        )
        # 新候補は追加されるが、生成中のユーザー選択は自動採用で上書きされない。
        assert len(panel2["image_candidates"]) == 3
        assert panel2["selected_candidate_id"] == user_pick


def test_generation_discards_candidate_when_input_changes(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)

        class EditingBackend:
            def __init__(self) -> None:
                self.count = 0

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                self.count += 1
                if self.count == 2:

                    def change_input(target_panel, target_page) -> None:
                        target_panel.prompt = "NEW_PROMPT"
                        target_panel.generation.prompt = "NEW_PROMPT"

                    update_panel_in_latest(app, project_id_, "p01_01", change_input)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (4, 5, 6)).save(target_path)
                return ImageResult("stub", "done", target_path, "old input result")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: EditingBackend()
        )
        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 2},
        )
        # 生成入力が変わってジョブがcancelledになると、同期生成APIは409を返す。
        assert response.status_code == 409
        detail = client.get(f"/api/projects/{project_id}").json()
        panel = detail["manga_json"]["pages"][0]["panels"][0]
        assert panel["prompt"] == "NEW_PROMPT"
        assert panel["image_candidates"] == []
        assert panel["selected_candidate_id"] is None
        assert panel["generation"]["status"] == "pending"
        jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        assert jobs[0]["status"] == "cancelled"
        assert jobs[0]["generation_input_hash"]


def test_candidate_selection_does_not_change_generation_input_hash(tmp_path: Path) -> None:
    """候補採用はgeneration_input_hashを変えないが、ユーザーのseed編集は検出すること。

    候補採用はseedを書き換えない（candidate.seedで表示）ためhash不変。一方seedは実際の
    生成入力なのでhashに含め、別タブ等でのseed編集は古い候補の混入として検出する。
    """
    export_dir = tmp_path / "exports"
    candidate = ImageCandidate(
        id="cand-a",
        asset="exports/project/panels/p01_01/cand-a.png",
        backend="comfyui",
        status="done",
        seed=999,
        created_at=db_now_utc(),
    )
    manga = MangaProject(
        title="hash",
        target_pages=4,
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0, 0, 1, 1),
                        shot="顔",
                        prompt="base",
                        image_candidates=[candidate],
                        generation=GenerationInfo(backend="stub", prompt="base", seed=1),
                    )
                ],
            )
        ],
    )
    panel = manga.pages[0].panels[0]
    before = generation_module.generation_input_hash(manga, panel, export_dir)
    # 既存候補を採用してもseedは基準seedのまま（candidate.seedで表示する）。
    generation_module.apply_candidate_selection(panel, candidate)
    assert panel.generation.seed == 1, "候補採用は基準seedを書き換えない"
    assert panel.selected_candidate_id == "cand-a"
    assert panel.generation.backend == "comfyui"  # backendは表示用に同期するがhash対象外
    after = generation_module.generation_input_hash(manga, panel, export_dir)
    assert before == after, "候補採用は生成入力の変更として扱わない"

    # ユーザーがseedを編集すると入力変更として検出される（古い候補の混入を防ぐ）。
    panel.generation.seed = 4242
    assert generation_module.generation_input_hash(manga, panel, export_dir) != after

    # 実プロンプトの変更もhashへ反映される。
    panel.generation.prompt = "changed"
    assert generation_module.generation_input_hash(manga, panel, export_dir) != after


def test_shutdown_keeps_queued_job_panels_consistent(tmp_path: Path, monkeypatch) -> None:
    """shutdown時、未開始queuedジョブのpanelをerrorにせずqueued+所有権のまま残すこと。

    複数ジョブのうち1件がgeneration_lockを取って生成中、残りがlock待ちqueuedの状態で
    正常終了すると、queuedジョブのpanelがerror化してDB(queued/再開対象)と食い違っていた。
    """
    import time

    class BlockingBackend:
        async def generate_panel(self, *args, **kwargs):
            await asyncio.Event().wait()  # cancelされるまで永久にブロックする

    monkeypatch.setattr(
        generation_module, "build_image_backend", lambda settings: BlockingBackend()
    )

    db_path = tmp_path / "test.db"
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(
            mutation_url(client, project_id, "generation-jobs"),
            json={"page": 1, "candidate_count": 1},
        )
        assert response.status_code == 200
        jobs = response.json()["result"]["jobs"]
        assert len(jobs) >= 2
        # 先頭ジョブがlock取得→set_running→generate_panelで停止し、残りはlock待ちqueuedになる。
        time.sleep(0.5)
        # ここでwithを抜けるとlifespan shutdown→JobManager.shutdown()が走る。

    factory = create_session_factory(f"sqlite:///{db_path}")
    with factory() as session:
        record = session.get(ProjectRecord, project_id)
        manga = MangaProject.model_validate(json.loads(record.manga_json))
        panels = {panel.panel_id: panel for page in manga.pages for panel in page.panels}
        statuses = []
        for job in jobs:
            db_job = session.get(GenerationJobRecord, job["id"])
            panel = panels[job["panel_id"]]
            statuses.append(db_job.status)
            if db_job.status == "queued":
                # 再開対象はqueuedのまま。所有権も維持し、Manga JSONをerrorにしない。
                assert panel.generation.status == "queued"
                assert panel.generation.active_job_id == job["id"]
            else:
                # 生成中だったジョブはshutdownがerror確定。panelもerrorで揃える。
                assert db_job.status == "error"
                assert panel.generation.status == "error"
        # 少なくとも1件はqueuedで再開待ちとして残ること（本テストの主眼）。
        assert "queued" in statuses


def test_selecting_candidate_midjob_does_not_discard_job(tmp_path: Path, monkeypatch) -> None:
    """生成中に基準seedと異なる既存候補を採用しても、進行中ジョブが破棄されないこと。"""
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)
        first = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 2},
        )
        assert first.status_code == 200
        candidates = first.json()["project"]["manga_json"]["pages"][0]["panels"][0][
            "image_candidates"
        ]
        assert len(candidates) == 2
        # 最小seedの既存候補を生成中に採用する。修正前は旧ジョブ選択でgeneration.seedが
        # 末尾候補のseedへ進むため、これを採用するとseed不一致でジョブが破棄されていた。
        other = min(candidates, key=lambda candidate: candidate["seed"])

        class SelectingBackend:
            def __init__(self) -> None:
                self.count = 0

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                self.count += 1
                if self.count == 1:

                    def reselect(target_panel, target_page) -> None:
                        candidate = next(
                            item for item in target_panel.image_candidates if item.id == other["id"]
                        )
                        generation_module.apply_candidate_selection(target_panel, candidate)

                    update_panel_in_latest(app, project_id_, "p01_01", reselect)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (1, 2, 3)).save(target_path)
                return ImageResult("stub", "done", target_path, "ok")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: SelectingBackend()
        )
        second = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 2},
        )
        # 候補採用ではseedが変わらないため、ジョブはcancelされず新候補が保存される。
        assert second.status_code == 200, second.text
        panel = second.json()["project"]["manga_json"]["pages"][0]["panels"][0]
        assert len(panel["image_candidates"]) == 4


def _queue_client(running: list[str], pending: list[str], posted: list[tuple[str, dict | None]]):
    class FakeComfyQueueClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return httpx.Response(
                200,
                json={
                    "queue_running": [[0, pid] for pid in running],
                    "queue_pending": [[0, pid] for pid in pending],
                },
                request=httpx.Request("GET", url),
            )

        def post(self, url, json=None):
            posted.append((url, json))
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    return FakeComfyQueueClient()


def test_stop_comfyui_removes_queued_without_global_interrupt(monkeypatch) -> None:
    settings = Settings(image_backend="comfyui")
    posted: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        generation_module.httpx, "Client", lambda *a, **k: _queue_client([], ["pid"], posted)
    )
    assert generation_module.stop_comfyui_generation(settings, "pid") == "queued_removed"
    assert any(url.endswith("/queue") and json == {"delete": ["pid"]} for url, json in posted)
    assert not any(url.endswith("/interrupt") for url, _ in posted)


def test_stop_comfyui_interrupts_only_when_running(monkeypatch) -> None:
    settings = Settings(image_backend="comfyui")
    posted: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        generation_module.httpx, "Client", lambda *a, **k: _queue_client(["pid"], [], posted)
    )
    assert generation_module.stop_comfyui_generation(settings, "pid") == "interrupted"
    assert any(url.endswith("/interrupt") for url, _ in posted)


def test_stop_comfyui_skips_unrelated_generation(monkeypatch) -> None:
    settings = Settings(image_backend="comfyui")
    posted: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        generation_module.httpx,
        "Client",
        lambda *a, **k: _queue_client(["other"], ["another"], posted),
    )
    # 対象prompt_idがキューに無ければグローバルinterruptもqueue削除もしない。
    assert generation_module.stop_comfyui_generation(settings, "pid") == "not_requested"
    assert posted == []


def test_cancel_completed_job_is_noop(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        # generate-imageはジョブ完了まで待つため、終了後にキャンセルする。
        generated = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        assert generated.status_code == 200
        jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        job = jobs[0]
        assert job["status"] == "done"

        cancelled = client.post(f"/api/generation-jobs/{job['id']}/cancel")
        assert cancelled.status_code == 200
        # 完了済みジョブのキャンセルは何もしない（doneのまま）。
        assert cancelled.json()["result"]["status"] == "done"
        # 成功したコマがskippedへ巻き戻らない。
        panel = next(
            p
            for p in client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "panels"
            ]
            if p["panel_id"] == "p01_01"
        )
        assert panel["generation"]["status"] != "skipped"
        assert panel["selected_candidate_id"]


def test_generate_image_and_cancel_return_latest_state(tmp_path: Path) -> None:
    """generate-image完了時・cancel時は「最新DB状態」を返し、latest_revisionが一致する。

    これらは完了後にcurrent_snapshotを読むため、返すprojectは操作時点ではなく最新state。
    latest_revisionで契約を固定し、UIが古いrevisionへ後退しないことを保証する。
    """
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        generated = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        assert generated.status_code == 200
        body = generated.json()
        # 完了時点の最新stateを返すので、latest_revision == project.revision。
        assert body["latest_revision"] == body["project"]["revision"]
        assert body["project"]["revision"] == current_revision(client, project_id)

        job = client.get(f"/api/projects/{project_id}/generation-jobs").json()[0]
        cancelled = client.post(f"/api/generation-jobs/{job['id']}/cancel")
        assert cancelled.status_code == 200
        cancelled_body = cancelled.json()
        assert cancelled_body["latest_revision"] == cancelled_body["project"]["revision"]
        assert cancelled_body["project"]["revision"] == current_revision(client, project_id)


def test_use_stub_creates_unique_candidate_files(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        client.post(mutation_url(client, project_id, "panels/p01_01/use-stub"))
        client.post(mutation_url(client, project_id, "panels/p01_01/use-stub"))
        panel = next(
            p
            for p in client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "panels"
            ]
            if p["panel_id"] == "p01_01"
        )
        assets = [candidate["asset"] for candidate in panel["image_candidates"]]
        assert len(assets) == 2
        # 候補ごとに別ファイルになっており、選び直しても画像が上書きされない。
        assert assets[0] != assets[1]
        assert all((tmp_path / "exports" / asset).exists() for asset in assets)


def test_auto_adopt_marks_page_pending(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        # ページ1をレンダリング済み(done)にする。
        assert client.post(mutation_url(client, project_id, "pages/1/render")).status_code == 200
        page1 = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert page1["render_status"] == "done"

        # ページ1のコマで候補を生成→自動採用されると、当該ページはpendingへ戻る。
        client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        page1_after = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert page1_after["render_status"] == "pending"


def test_workflow_json_is_patched_for_panel(tmp_path: Path) -> None:
    workflow = sample_workflow()
    config = workflow_config(tmp_path)
    panel = Panel(
        panel_id="p01_01",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="テスト",
        prompt="元prompt",
        generation=GenerationInfo(
            prompt="差し替えprompt", negative_prompt="bad", seed=42, width=960, height=540
        ),
    )
    patched = apply_panel_to_workflow(workflow, config, panel, "prefix/test")
    assert (
        patched["6"]["inputs"]["text"]
        == "差し替えprompt, no text, no speech bubble, no watermark, no manga panel text"
    )
    assert patched["7"]["inputs"]["text"] == "bad"
    assert patched["3"]["inputs"]["seed"] == 42
    assert patched["5"]["inputs"]["width"] == 960
    assert patched["5"]["inputs"]["height"] == 540
    assert patched["9"]["inputs"]["filename_prefix"] == "prefix/test"


def test_workflow_json_applies_character_lora(tmp_path: Path) -> None:
    workflow = sample_workflow()
    workflow["20"] = {"class_type": "LoraLoader", "inputs": {"lora_name": "old.safetensors"}}
    panel = Panel(
        panel_id="p01_01",
        bbox=(0, 0, 1, 1),
        shot="テスト",
        generation=GenerationInfo(
            loras=[
                {
                    "node_id": "20",
                    "lora_name": "character.safetensors",
                    "strength_model": 0.8,
                    "strength_clip": 0.7,
                }
            ]
        ),
    )
    patched = apply_panel_to_workflow(workflow, workflow_config(tmp_path), panel, "prefix")
    assert patched["20"]["inputs"]["lora_name"] == "character.safetensors"
    assert patched["20"]["inputs"]["strength_model"] == 0.8
    assert patched["20"]["inputs"]["strength_clip"] == 0.7


def test_workflow_preset_patches_model_and_sampler(tmp_path: Path) -> None:
    workflow = sample_workflow()
    workflow["40"] = {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "old.safetensors"},
    }
    workflow["41"] = {"class_type": "VAELoader", "inputs": {"vae_name": "old.vae"}}
    panel = Panel(
        panel_id="p01_01",
        bbox=(0, 0, 1, 1),
        shot="test",
        generation=GenerationInfo(
            workflow_preset={
                "id": "anime",
                "name": "anime",
                "checkpoint_node_id": "40",
                "checkpoint_name": "anime.safetensors",
                "vae_node_id": "41",
                "vae_name": "anime.vae",
                "sampler_node_id": "3",
                "sampler_name": "euler",
                "scheduler": "normal",
                "steps": 24,
                "cfg": 5.5,
                "denoise": 0.9,
            }
        ),
    )
    patched = apply_panel_to_workflow(workflow, workflow_config(tmp_path), panel, "prefix")
    assert patched["40"]["inputs"]["ckpt_name"] == "anime.safetensors"
    assert patched["41"]["inputs"]["vae_name"] == "anime.vae"
    assert patched["3"]["inputs"]["steps"] == 24
    assert patched["3"]["inputs"]["cfg"] == 5.5


def test_existing_manga_json_gets_new_defaults() -> None:
    manga = MangaProject.model_validate(
        {
            "title": "旧形式",
            "target_pages": 4,
            "pages": [
                {
                    "page": 1,
                    "theme": "旧形式テスト",
                    "layout_template": "one",
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0.0, 0.0, 1.0, 1.0],
                            "shot": "テスト",
                            "dialogue": [{"speaker": "char_a", "text": "長い台詞"}],
                        }
                    ],
                }
            ],
        }
    )
    panel = manga.pages[0].panels[0]
    assert panel.generation.fit_mode == "cover"
    assert panel.generation.text_policy == "no_text"
    assert panel.generation.crop_scale == 1.0
    assert panel.subject_mode == "character_scene"
    assert panel.dialogue[0].font_size is None
    assert manga.typography.default_font_size == 34
    assert panel.dialogue[0].vertical is True
    # 旧形式は吹き出し既定が無いので新既定のovalになる。
    assert panel.dialogue[0].balloon == "oval"
    assert panel.dialogue[0].box is None
    assert manga.common_positive_prompt == ""
    assert manga.common_negative_prompt == ""
    assert manga.characters == []


def test_comfyui_status_reports_missing_workflow(tmp_path: Path) -> None:
    with make_client(tmp_path, image_backend="comfyui") as client:
        response = client.get("/api/comfyui/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["workflow_exists"] is False
        assert "見つかりません" in payload["message"]


def test_mock_comfyui_generates_single_panel_image(tmp_path: Path, monkeypatch) -> None:
    workflow_path = tmp_path / "workflow_api.json"
    workflow_path.write_text(json.dumps(sample_workflow()), encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.image_backends.httpx.AsyncClient", lambda *args, **kwargs: MockComfyUIClient()
    )

    with make_client(tmp_path, image_backend="comfyui", workflow_path=workflow_path) as client:
        project_id = create_generated_project(client)
        response = client.get("/api/comfyui/status")
        assert response.status_code == 200
        assert response.json()["connected"] is True

        response = client.post(mutation_url(client, project_id, "panels/p01_01/generate-image"))
        assert response.status_code == 200
        first_panel = response.json()["project"]["manga_json"]["pages"][0]["panels"][0]
        assert first_panel["generation"]["status"] == "done"
        assert first_panel["generation"]["prompt_id"] == "prompt-1"
        assert (tmp_path / "exports" / first_panel["image_asset"]).exists()


def test_single_panel_stub_endpoint_updates_panel(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(mutation_url(client, project_id, "panels/p01_01/use-stub"))
        assert response.status_code == 200
        first_panel = response.json()["project"]["manga_json"]["pages"][0]["panels"][0]
        assert first_panel["generation"]["backend"] == "stub"
        assert first_panel["generation"]["status"] == "done"
        assert first_panel["image_asset"]


def test_single_panel_render_page_endpoint_updates_page_png(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(mutation_url(client, project_id, "panels/p01_01/use-stub"))
        assert response.status_code == 200
        response = client.post(mutation_url(client, project_id, "panels/p01_01/render-page"))
        assert response.status_code == 200
        page_asset = response.json()["result"]["page_asset"]
        assert page_asset.startswith(f"{project_id}/pages/page_001.")
        assert page_asset.endswith(".png")
        assert (tmp_path / "exports" / page_asset).exists()


def test_generation_job_creates_candidates_and_selects_one(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generation-jobs"),
            json={"candidate_count": 2},
        )
        assert response.status_code == 200
        job_id = response.json()["result"]["id"]

        with client.websocket_connect(f"/api/generation-jobs/{job_id}/ws") as websocket:
            while True:
                job = websocket.receive_json()
                if job["status"] in {"done", "error", "cancelled"}:
                    break

        assert job["status"] == "done"
        assert job["progress"] == 100
        assert len(job["candidate_ids"]) == 2
        project = client.get(f"/api/projects/{project_id}").json()
        panel = project["manga_json"]["pages"][0]["panels"][0]
        assert len(panel["image_candidates"]) == 2
        assert panel["image_candidates"][0]["asset"] != panel["image_candidates"][1]["asset"]
        assert panel["image_candidates"][0]["seed"] + 1 == panel["image_candidates"][1]["seed"]
        assert panel["selected_candidate_id"] == panel["image_candidates"][1]["id"]
        assert "masterpiece" in panel["image_candidates"][0]["prompt"]
        assert panel["image_candidates"][0]["characters"] == ["char_a", "char_b"]

        first_candidate_id = panel["image_candidates"][0]["id"]
        response = client.post(
            mutation_url(
                client, project_id, f"panels/p01_01/candidates/{first_candidate_id}/select"
            )
        )
        assert response.status_code == 200
        selected_panel = response.json()["project"]["manga_json"]["pages"][0]["panels"][0]
        assert selected_panel["selected_candidate_id"] == first_candidate_id
        assert response.json()["result"]["page_asset"].startswith(f"{project_id}/pages/page_001.")


def test_select_candidate_render_conflict_returns_partial_success(
    tmp_path: Path, monkeypatch
) -> None:
    """候補採用後のページrender競合は、採用済みstateを持つ部分成功契約で返す。

    通常のrevision競合に偽装せず、候補採用が確定済みであることをフロントへ伝える。
    """
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        job = client.post(
            mutation_url(client, project_id, "panels/p01_01/generation-jobs"),
            json={"candidate_count": 1},
        ).json()["result"]
        with client.websocket_connect(f"/api/generation-jobs/{job['id']}/ws") as websocket:
            while websocket.receive_json()["status"] not in {"done", "error", "cancelled"}:
                pass
        panel = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
            "panels"
        ][0]
        candidate_id = panel["image_candidates"][0]["id"]

        # 候補採用は成功させ、その後のページrenderだけ競合させる。
        monkeypatch.setattr(
            rendering_module.RenderingService,
            "render_and_commit_page",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(
                rendering_module.RenderInputChangedError()
            ),
        )
        response = client.post(
            mutation_url(client, project_id, f"panels/p01_01/candidates/{candidate_id}/select")
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "project_mutation_partially_applied"
        assert body["completed_operation"] == "candidate_selection"
        assert body["failed_operation"] == "render_page"
        # completed_projectは候補採用を確定したsnapshot。
        completed_panel = body["completed_project"]["manga_json"]["pages"][0]["panels"][0]
        assert completed_panel["selected_candidate_id"] == candidate_id
        # projectは応答時点の最新DB state、latest_revisionも同梱する。
        returned_panel = body["project"]["manga_json"]["pages"][0]["panels"][0]
        assert returned_panel["selected_candidate_id"] == candidate_id
        assert body["latest_revision"] == body["project"]["revision"]
        # 同時更新がない通常ケースでは、completedとprojectのrevisionは一致する。
        assert body["completed_project"]["revision"] == body["project"]["revision"]
        # 実DBにも採用が確定している。
        persisted = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
            "panels"
        ][0]
        assert persisted["selected_candidate_id"] == candidate_id


def test_select_candidate_partial_success_reports_latest_after_concurrent_update(
    tmp_path: Path, monkeypatch
) -> None:
    """候補採用→別更新→render競合で、partial-successが最新stateとlatest_revisionを返す。

    部分成功は同時更新が原因なので、projectはcompleted_projectより新しいrevisionになり、
    フロントは最新へ再同期できる。
    """
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        job = client.post(
            mutation_url(client, project_id, "panels/p01_01/generation-jobs"),
            json={"candidate_count": 1},
        ).json()["result"]
        with client.websocket_connect(f"/api/generation-jobs/{job['id']}/ws") as websocket:
            while websocket.receive_json()["status"] not in {"done", "error", "cancelled"}:
                pass
        panel = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
            "panels"
        ][0]
        candidate_id = panel["image_candidates"][0]["id"]

        def advance_then_fail(self, *args, **kwargs):
            # 候補採用確定後・render前に別操作がrevisionを進める。
            app.state.mutation.mutate_local(
                project_id, lambda manga: setattr(manga, "premise", "別タブ更新")
            )
            raise rendering_module.RenderInputChangedError()

        monkeypatch.setattr(
            rendering_module.RenderingService, "render_and_commit_page", advance_then_fail
        )
        response = client.post(
            mutation_url(client, project_id, f"panels/p01_01/candidates/{candidate_id}/select")
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "project_mutation_partially_applied"
        # projectは最新(別更新後)で、completed_projectより新しい。latest_revisionも最新。
        assert body["project"]["revision"] > body["completed_project"]["revision"]
        assert body["latest_revision"] == body["project"]["revision"]
        # レスポンス内の単調性: completed <= latest が常に成り立つ（単一readで構成）。
        assert body["completed_project"]["revision"] <= body["latest_revision"]
        assert body["project"]["revision"] <= body["latest_revision"]
        assert body["project"]["manga_json"]["premise"] == "別タブ更新"
        # 候補採用自体は最新stateにも残っている。
        latest_panel = body["project"]["manga_json"]["pages"][0]["panels"][0]
        assert latest_panel["selected_candidate_id"] == candidate_id


def test_job_manager_cancels_running_task() -> None:
    async def scenario() -> None:
        manager = JobManager()
        job = manager.create("project", "panel", 1)

        async def wait_forever() -> None:
            await asyncio.sleep(60)

        manager.start(job, wait_forever())
        manager.update(job, status="running")
        manager.cancel(job)
        await asyncio.sleep(0)
        assert job.status == "cancelled"
        assert manager.tasks[job.id].cancelled()

    asyncio.run(scenario())


def test_cancel_before_task_start_releases_panel_and_allows_regeneration(
    tmp_path: Path, monkeypatch
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manager = client.app.state.job_manager

        def do_not_start(job, coroutine) -> None:
            coroutine.close()

        monkeypatch.setattr(manager, "start", do_not_start)
        created = client.post(
            mutation_url(client, project_id, "panels/p01_01/generation-jobs"),
            json={"candidate_count": 1},
        )
        assert created.status_code == 200
        cancelled = client.post(f"/api/generation-jobs/{created.json()['result']['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["result"]["status"] == "cancelled"
        detail = client.get(f"/api/projects/{project_id}").json()
        panel = detail["manga_json"]["pages"][0]["panels"][0]
        assert panel["generation"]["status"] == "skipped"
        revision_after_cancel = detail["revision"]
        cancelled_job = manager.get(created.json()["result"]["id"])
        assert cancelled_job is not None
        mark_panel_job_stopped(client.app, cancelled_job, "生成をキャンセルしました")
        assert client.get(f"/api/projects/{project_id}").json()["revision"] == revision_after_cancel

        retried = client.post(
            mutation_url(client, project_id, "panels/p01_01/generation-jobs"),
            json={"candidate_count": 1},
        )
        assert retried.status_code == 200


def test_delayed_old_job_stop_does_not_overwrite_new_job_panel(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        manager = app.state.job_manager

        old_job = app.state.generation.enqueue(project_id, ["p01_01"], 1, "旧ジョブ")[0]
        manager.register_in_memory(old_job)
        assert manager.cancel(old_job)
        mark_panel_job_stopped(app, old_job, "生成をキャンセルしました")

        new_job = app.state.generation.enqueue(project_id, ["p01_01"], 1, "新ジョブ")[0]
        manager.register_in_memory(new_job)
        before = client.get(f"/api/projects/{project_id}").json()
        panel_before = before["manga_json"]["pages"][0]["panels"][0]
        assert panel_before["generation"]["status"] == "queued"
        assert panel_before["generation"]["active_job_id"] == new_job.id

        # 旧TaskのCancelledError処理が後着しても、新jobの所有状態は変更しない。
        mark_panel_job_stopped(app, old_job, "生成をキャンセルしました")
        after = client.get(f"/api/projects/{project_id}").json()
        panel_after = after["manga_json"]["pages"][0]["panels"][0]
        assert after["revision"] == before["revision"]
        assert panel_after["generation"]["status"] == "queued"
        assert panel_after["generation"]["active_job_id"] == new_job.id


def test_cancelled_job_stays_cancelled_when_backend_raises_late(
    tmp_path: Path, monkeypatch
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        started = threading.Event()

        class LateFailingBackend:
            async def generate_panel(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError as exc:
                    raise RuntimeError("キャンセル後の遅延例外") from exc

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: LateFailingBackend()
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                mutation_url(client, project_id, "panels/p01_01/generate-image"),
                json={"candidate_count": 1},
            )
            assert started.wait(timeout=10)
            jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
            cancelled = client.post(f"/api/generation-jobs/{jobs[0]['id']}/cancel")
            generated = future.result(timeout=10)
        assert cancelled.json()["result"]["status"] == "cancelled"
        assert generated.status_code == 409
        persisted = client.get(f"/api/projects/{project_id}/generation-jobs").json()[0]
        assert persisted["status"] == "cancelled"
        panel = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
            "panels"
        ][0]
        assert panel["generation"]["status"] == "skipped"


def test_shutdown_marks_running_job_error_not_queued(tmp_path: Path) -> None:
    # 正常shutdown時、ComfyUI投入済みかもしれないrunningジョブをqueuedで残すと
    # 次回起動で二重投入される。errorにしてprompt_idもクリアすることを確認する。
    async def scenario() -> None:
        session_factory = create_session_factory(f"sqlite:///{tmp_path / 'jobs.db'}")
        with session_factory() as session:
            session.add(
                ProjectRecord(
                    id="project",
                    title="t",
                    work_name="",
                    manga_json="{}",
                    created_at=db_now_utc(),
                    updated_at=db_now_utc(),
                )
            )
            session.commit()
        manager = JobManager(session_factory)
        job = manager.create("project", "panel", 1)

        async def wait_forever() -> None:
            await asyncio.sleep(60)

        manager.start(job, wait_forever())
        manager.update(job, status="running", prompt_id="remote-prompt")
        await manager.shutdown()
        recovered = manager.get(job.id)
        assert recovered is not None
        assert recovered.status == "error"
        assert recovered.prompt_id is None

    asyncio.run(scenario())


def test_use_stub_marks_page_pending(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        assert client.post(mutation_url(client, project_id, "pages/1/render")).status_code == 200
        assert (
            client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "render_status"
            ]
            == "done"
        )
        # use-stubで採用画像が変わったら、対象ページは再レンダリング対象(pending)へ戻る。
        assert (
            client.post(mutation_url(client, project_id, "panels/p01_01/use-stub")).status_code
            == 200
        )
        assert (
            client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "render_status"
            ]
            == "pending"
        )


def test_cbz_returns_revision_and_client_can_sync(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        assert client.post(mutation_url(client, project_id, "render")).status_code == 200
        before = client.get(f"/api/projects/{project_id}").json()["revision"]
        export = client.post(mutation_url(client, project_id, "export/cbz"))
        assert export.status_code == 200
        # CBZ出力もrevisionを進め、応答で返すのでクライアントが同期できる。
        exported_revision = export.json()["project"]["revision"]
        assert exported_revision > before
        assert client.get(f"/api/projects/{project_id}").json()["revision"] == exported_revision


def test_failed_staged_render_does_not_replace_canonical_page(tmp_path: Path, monkeypatch) -> None:
    export_dir = tmp_path / "exports"
    canonical = export_dir / "project" / "pages" / "page_001.png"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"old-page")

    def fail_after_staging(project_id, manga, page_number, root, *, output_dir=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "page_001.png").write_bytes(b"incomplete-page")
        raise RuntimeError("描画失敗")

    monkeypatch.setattr(rendering_module, "render_project_page", fail_after_staging)
    with pytest.raises(RuntimeError, match="描画失敗"):
        rendering_module.render_snapshot_page(
            "project", MangaProject(title="test"), 1, export_dir, revision=3
        )
    assert canonical.read_bytes() == b"old-page"
    assert not list((export_dir / "project" / ".render-staging").glob("revision-*"))


def test_page_render_conflict_cleans_its_unreferenced_png(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        monkeypatch.setattr(
            rendering_module.RenderingService,
            "commit_rendered_pages",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(
                rendering_module.RenderInputChangedError()
            ),
        )
        response = client.post(mutation_url(client, project_id, "pages/1/render"))
        assert response.status_code == 409
        assert not list((tmp_path / "exports" / project_id / "pages").glob("page_001.*.png"))


def test_render_cleanup_preserves_asset_referenced_by_revision(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        detail = client.get(f"/api/projects/{project_id}").json()
        snapshot = MangaProject.model_validate(detail["manga_json"])
        ownership: dict[Path, bool] = {}
        asset, _warnings = rendering_module.render_snapshot_page(
            project_id,
            snapshot,
            1,
            app.state.settings.export_dir,
            detail["revision"],
            ownership=ownership,
        )
        snapshot.pages[0].render_status = "done"
        snapshot.pages[0].render_hash = rendering_module.page_render_hash(
            snapshot, snapshot.pages[0]
        )
        snapshot.pages[0].render_asset = rendering_module.asset_to_id(
            asset, app.state.settings.export_dir
        )
        with app.state.SessionLocal() as session:
            session.add(
                ProjectRevisionRecord(
                    id="history-render",
                    project_id=project_id,
                    label="描画履歴",
                    manga_json=snapshot.model_dump_json(),
                    created_at=db_now_utc(),
                )
            )
            session.commit()
        app.state.rendering.cleanup_published_assets(project_id, ownership)
        assert asset.is_file()


def test_render_exception_keeps_page_pending(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        monkeypatch.setattr(
            rendering_module,
            "render_project_page",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("描画失敗")),
        )
        with pytest.raises(RuntimeError, match="描画失敗"):
            client.post(mutation_url(client, project_id, "pages/1/render"))
        page = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert page["render_status"] == "pending"
        assert page["render_asset"] is None


def test_old_render_cannot_overwrite_new_render(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        detail_a = client.get(f"/api/projects/{project_id}").json()
        snapshot_a = MangaProject.model_validate(detail_a["manga_json"])
        asset_a, _ = rendering_module.render_snapshot_page(
            project_id, snapshot_a, 1, app.state.settings.export_dir, detail_a["revision"]
        )

        changed = detail_a["manga_json"]
        changed["pages"][0]["panels"][0]["bbox"][0] += 0.001
        saved = put_manga(client, project_id, changed).json()["project"]
        snapshot_b = MangaProject.model_validate(saved["manga_json"])
        asset_b, _ = rendering_module.render_snapshot_page(
            project_id, snapshot_b, 1, app.state.settings.export_dir, saved["revision"]
        )
        manga_b, _revision = commit_rendered_pages(
            app,
            project_id,
            snapshot_b.model_copy(update={"pages": [snapshot_b.pages[0]]}),
            [asset_b],
        )
        with pytest.raises(HTTPException) as conflict:
            commit_rendered_pages(
                app,
                project_id,
                snapshot_a.model_copy(update={"pages": [snapshot_a.pages[0]]}),
                [asset_a],
            )
        assert conflict.value.status_code == 409
        assert asset_a != asset_b
        assert asset_a.exists() and asset_b.exists()
        assert manga_b.pages[0].render_asset.endswith(asset_b.name)


def test_overlay_reupload_rejects_old_render_snapshot(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        manga["pages"][0]["overlay_elements"] = [
            {"id": "same", "source_panel_id": "p01_01", "box": [0.1, 0.1, 0.3, 0.3]}
        ]
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert response.status_code == 200

        def upload(color: str):
            buffer = io.BytesIO()
            Image.new("RGBA", (16, 16), color).save(buffer, format="PNG")
            return client.post(
                f"/api/projects/{project_id}/pages/1/overlays/same/asset"
                f"?revision={current_revision(client, project_id)}",
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )

        red = upload("red").json()
        old_snapshot = MangaProject.model_validate(red["project"]["manga_json"])
        old_asset, _ = rendering_module.render_snapshot_page(
            project_id,
            old_snapshot,
            1,
            app.state.settings.export_dir,
            red["project"]["revision"],
        )
        green = upload("green").json()
        assert red["result"]["asset"] != green["result"]["asset"]

        with pytest.raises(HTTPException) as conflict:
            commit_rendered_pages(
                app,
                project_id,
                old_snapshot.model_copy(update={"pages": [old_snapshot.pages[0]]}),
                [old_asset],
            )
        assert conflict.value.status_code == 409
        current = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert current["render_status"] == "pending"

        rendered = client.post(mutation_url(client, project_id, "pages/1/render"))
        assert rendered.status_code == 200
        rendered_page = rendered.json()["project"]["manga_json"]["pages"][0]
        assert rendered_page["render_status"] == "done"
        assert green["result"]["asset"] in json.dumps(rendered_page, ensure_ascii=False)


def test_cbz_failure_keeps_pages_pending(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        project_dir = tmp_path / "exports" / project_id
        pngs_before = set(project_dir.rglob("*.png")) if project_dir.exists() else set()
        monkeypatch.setattr(
            project_render_module,
            "export_confirmed_cbz",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("CBZ失敗")),
        )
        with pytest.raises(RuntimeError, match="CBZ失敗"):
            client.post(mutation_url(client, project_id, "export/cbz"))
        pages = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"]
        assert all(page["render_status"] == "pending" for page in pages)
        # CBZ生成が例外でも、先に公開したPNGは回収され残らない。
        pngs_after = set(project_dir.rglob("*.png")) if project_dir.exists() else set()
        assert pngs_after == pngs_before


def test_cbz_render_conflict_removes_uncommitted_archive(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        monkeypatch.setattr(
            rendering_module.RenderingService,
            "commit_rendered_pages",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(
                rendering_module.RenderInputChangedError()
            ),
        )
        response = client.post(mutation_url(client, project_id, "export/cbz"))
        assert response.status_code == 409
        assert not list((tmp_path / "exports" / project_id).glob("*.cbz"))


def test_cbz_rejects_newer_invalid_reading_order_snapshot(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        original_export = project_render_module.export_confirmed_cbz

        def update_then_export(*args, **kwargs):
            def break_reading_order(manga: MangaProject) -> None:
                manga.pages[0].reading_order = ["missing-panel"]

            app.state.mutation.mutate_local(project_id, break_reading_order)
            return original_export(*args, **kwargs)

        monkeypatch.setattr(project_render_module, "export_confirmed_cbz", update_then_export)
        response = client.post(mutation_url(client, project_id, "export/cbz"))
        assert response.status_code == 409
        latest_preflight = client.post(f"/api/projects/{project_id}/preflight").json()
        assert any(issue["code"] == "invalid_reading_order" for issue in latest_preflight["errors"])
        assert not list((tmp_path / "exports" / project_id).glob("*.cbz"))


def test_concurrent_cbz_conflict_preserves_successful_assets(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        barrier = threading.Barrier(2)
        original_export = project_render_module.export_confirmed_cbz

        def synchronized_export(*args, **kwargs):
            path = original_export(*args, **kwargs)
            barrier.wait(timeout=10)
            return path

        monkeypatch.setattr(project_render_module, "export_confirmed_cbz", synchronized_export)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _: client.post(mutation_url(client, project_id, "export/cbz")),
                    range(2),
                )
            )
        assert sorted(response.status_code for response in responses) == [200, 409]
        successful = next(response.json() for response in responses if response.status_code == 200)
        cbz_path = tmp_path / "exports" / successful["result"]["cbz_asset"]
        assert cbz_path.is_file()
        for page in successful["project"]["manga_json"]["pages"]:
            assert (tmp_path / "exports" / page["render_asset"]).is_file()
        status = client.get(f"/api/projects/{project_id}/production-status").json()
        assert all(page["rendered"] for page in status["pages"])


def test_production_status_detects_missing_render_asset(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        rendered = client.post(mutation_url(client, project_id, "pages/1/render")).json()
        asset = tmp_path / "exports" / rendered["result"]["page_asset"]
        asset.unlink()
        status = client.get(f"/api/projects/{project_id}/production-status").json()
        page = next(item for item in status["pages"] if item["page"] == 1)
        assert page["rendered"] is False
        saved_page = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert saved_page["render_status"] == "pending"


def test_page_render_response_uses_its_committed_snapshot(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        original = projects_router.to_project_mutation_response

        def interleave_update(request, target_id, result, *, snapshot=None):
            assert snapshot is not None
            app.state.mutation.mutate_local(
                project_id, lambda manga: setattr(manga, "premise", "応答整形前の別更新")
            )
            return original(request, target_id, result, snapshot=snapshot)

        monkeypatch.setattr(projects_router, "to_project_mutation_response", interleave_update)
        response = client.post(mutation_url(client, project_id, "pages/1/render"))
        assert response.status_code == 200
        body = response.json()
        latest = client.get(f"/api/projects/{project_id}").json()
        assert body["project"]["revision"] + 1 == latest["revision"]
        page = body["project"]["manga_json"]["pages"][0]
        assert page["render_asset"] == body["result"]["page_asset"]
        assert body["project"]["manga_json"]["premise"] != latest["manga_json"]["premise"]


def test_mutation_service_cas_detects_conflict(tmp_path: Path) -> None:
    from backend.app.mutation import ProjectConflictError, ProjectMutationService

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'mut.db'}")
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title="t",
                work_name="",
                manga_json=MangaProject(title="t").model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    service = ProjectMutationService(session_factory, tmp_path / "exports")

    # 期待revision不一致は409相当(ProjectConflictError)。
    with pytest.raises(ProjectConflictError):
        service.mutate_user("p", expected_revision=99, mutate=lambda manga: None)

    # 正しい期待revisionなら適用し、revisionを進める。
    mutation_result = service.mutate_user(
        "p", expected_revision=0, mutate=lambda manga: setattr(manga, "premise", "x")
    )
    assert mutation_result.project.revision == 1


def test_mutation_service_epoch_mismatch(tmp_path: Path) -> None:
    from backend.app.mutation import EpochMismatchError, ProjectMutationService

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'epoch.db'}")
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title="t",
                work_name="",
                manga_json=MangaProject(title="t").model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    service = ProjectMutationService(session_factory, tmp_path / "exports")
    # 世代不一致(構成全置換後の古いジョブ相当)はEpochMismatchError。
    with pytest.raises(EpochMismatchError):
        service.mutate_worker("p", expected_epoch=5, mutate=lambda manga: None)


def test_structural_replace_saves_history_and_epoch_atomically(tmp_path: Path) -> None:
    from backend.app.mutation import ProjectConflictError, ProjectMutationService

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'structural.db'}")
    original = MangaProject(title="変更前", premise="旧構成")
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title=original.title,
                work_name="",
                manga_json=original.model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    service = ProjectMutationService(session_factory, tmp_path / "exports")

    with pytest.raises(ProjectConflictError):
        service.replace_with_history(
            "p",
            lambda _session, manga: manga,
            expected_revision=9,
            history_label="保存されない履歴",
        )
    with session_factory() as session:
        assert session.query(ProjectRevisionRecord).count() == 0

    mutation_result = service.replace_with_history(
        "p",
        lambda _session, manga: manga.model_copy(update={"title": "変更後", "premise": "新構成"}),
        expected_revision=0,
        history_label="変更前",
    )
    replacement = mutation_result.project.manga
    assert replacement.title == "変更後"
    assert mutation_result.project.revision == 1
    with session_factory() as session:
        project = session.get(ProjectRecord, "p")
        history = session.query(ProjectRevisionRecord).one()
        assert project.revision == 1
        assert project.generation_epoch == 1
        assert MangaProject.model_validate_json(history.manga_json).premise == "旧構成"


def test_mutate_user_conflict_does_not_retry_and_returns_snapshot(tmp_path: Path) -> None:
    from sqlalchemy.orm import Session

    from backend.app.mutation import ProjectConflictError, ProjectMutationService
    from backend.app.repository import ProjectRepository

    class FailingCasRepository(ProjectRepository):
        def __init__(self) -> None:
            self.calls = 0

        def cas_set_manga(
            self,
            session: Session,
            project_id: str,
            base_revision: int,
            manga: MangaProject,
            **kwargs,
        ) -> int:
            self.calls += 1
            return 0

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'user-no-retry.db'}")
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title="t",
                work_name="",
                manga_json=MangaProject(title="t").model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()

    repository = FailingCasRepository()
    service = ProjectMutationService(session_factory, tmp_path / "exports", repository)
    with pytest.raises(ProjectConflictError):
        service.mutate_user(
            "p", expected_revision=0, mutate=lambda manga: setattr(manga, "premise", "x")
        )
    assert repository.calls == 1

    normal = ProjectMutationService(session_factory, tmp_path / "exports")
    result = normal.mutate_user(
        "p", expected_revision=0, mutate=lambda manga: setattr(manga, "premise", "ok")
    )
    assert result.project.project_id == "p"
    assert result.project.revision == 1
    assert result.project.generation_epoch == 0
    assert result.project.manga.premise == "ok"


def test_worker_panel_mutation_cannot_change_other_panel(tmp_path: Path) -> None:
    from backend.app.mutation import (
        ProjectMutationService,
        WorkerScopeViolationError,
        mark_page_dirty,
    )

    manga = MangaProject(
        title="scope",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="two",
                reading_order=["p1", "p2"],
                panels=[
                    Panel(panel_id="p1", bbox=(0.0, 0.0, 0.5, 1.0), shot="a"),
                    Panel(panel_id="p2", bbox=(0.5, 0.0, 0.5, 1.0), shot="b"),
                ],
            )
        ],
    )
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'worker-scope.db'}")
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title=manga.title,
                work_name="",
                manga_json=manga.model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    service = ProjectMutationService(session_factory, tmp_path / "exports")

    def break_scope(_manga: MangaProject, _panel: Panel, page: Page) -> None:
        page.panels[1].prompt = "対象外変更"

    with pytest.raises(WorkerScopeViolationError):
        service.mutate_worker_panel("p", panel_id="p1", expected_epoch=0, mutate=break_scope)

    # ページ構造・overlay等への変更も拒否する。
    def break_page_scope(_manga: MangaProject, _panel: Panel, page: Page) -> None:
        page.theme = "別テーマ"

    with pytest.raises(WorkerScopeViolationError):
        service.mutate_worker_panel("p", panel_id="p1", expected_epoch=0, mutate=break_page_scope)

    # 対象panel自身とページのrender状態(render_status系)の変更は許可する。
    def in_scope(_manga: MangaProject, panel: Panel, page: Page) -> str:
        panel.prompt = "対象内変更"
        mark_page_dirty(page)
        return "ok"

    result = service.mutate_worker_panel("p", panel_id="p1", expected_epoch=0, mutate=in_scope)
    assert result.result == "ok"


def test_worker_panel_mutation_cannot_touch_other_pages(tmp_path: Path) -> None:
    """対象panelの所属ページ以外（別ページのrender状態偽装・panel移動）も拒否する。"""
    from backend.app.mutation import (
        ProjectMutationService,
        WorkerScopeViolationError,
        mark_page_dirty,
    )

    manga = MangaProject(
        title="scope",
        pages=[
            Page(
                page=1,
                theme="t1",
                layout_template="single",
                reading_order=["p1"],
                panels=[Panel(panel_id="p1", bbox=(0.0, 0.0, 1.0, 1.0), shot="a")],
            ),
            Page(
                page=2,
                theme="t2",
                layout_template="single",
                reading_order=["p2"],
                panels=[Panel(panel_id="p2", bbox=(0.0, 0.0, 1.0, 1.0), shot="b")],
            ),
        ],
    )
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'worker-scope2.db'}")
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title=manga.title,
                work_name="",
                manga_json=manga.model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    service = ProjectMutationService(session_factory, tmp_path / "exports")

    # 別ページのrender状態をdoneへ偽装する変更は拒否する。
    def forge_other_page_done(manga: MangaProject, _panel: Panel, _page: Page) -> None:
        manga.pages[1].render_status = "done"
        manga.pages[1].render_asset = "assets/forged.png"
        manga.pages[1].render_hash = "deadbeef"

    with pytest.raises(WorkerScopeViolationError):
        service.mutate_worker_panel(
            "p", panel_id="p1", expected_epoch=0, mutate=forge_other_page_done
        )

    # 対象panelを別ページへ移動する変更も拒否する。
    def move_target_panel(_manga: MangaProject, panel: Panel, page: Page) -> None:
        page.panels.remove(panel)
        _manga.pages[1].panels.append(panel)

    with pytest.raises(WorkerScopeViolationError):
        service.mutate_worker_panel("p", panel_id="p1", expected_epoch=0, mutate=move_target_panel)

    # 所属ページ(page=1)自身のrender状態変更は許可する。
    def dirty_owner_page(_manga: MangaProject, _panel: Panel, page: Page) -> str:
        mark_page_dirty(page)
        return "ok"

    result = service.mutate_worker_panel(
        "p", panel_id="p1", expected_epoch=0, mutate=dirty_owner_page
    )
    assert result.result == "ok"


def test_generation_service_runs_without_fastapi_app(tmp_path: Path) -> None:
    from backend.app.generation_service import GenerationRuntime, GenerationService
    from backend.app.mutation import ProjectMutationService
    from backend.app.rendering import RenderingService
    from backend.app.repository import ProjectRepository

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'generation-runtime.db'}",
        export_dir=tmp_path / "exports",
        image_backend="stub",
    )
    session_factory = create_session_factory(settings.database_url)
    manga = MangaProject(
        title="runtime",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                reading_order=["p1"],
                panels=[Panel(panel_id="p1", bbox=(0.0, 0.0, 1.0, 1.0), shot="a")],
            )
        ],
    )
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title=manga.title,
                work_name="",
                manga_json=manga.model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    repository = ProjectRepository()
    mutation = ProjectMutationService(session_factory, settings.export_dir, repository)
    jobs = JobManager(session_factory)
    rendering = RenderingService(session_factory, settings.export_dir, mutation, repository)
    service = GenerationService(
        session_factory,
        settings.export_dir,
        GenerationRuntime(settings, jobs, mutation, rendering, repository),
    )

    job = service.enqueue("p", ["p1"], 1, "登録")[0]
    jobs.register_in_memory(job)
    service.mark_panel_job_stopped(job, "停止")
    with session_factory() as session:
        latest = MangaProject.model_validate_json(session.get(ProjectRecord, "p").manga_json)
    assert latest.pages[0].panels[0].generation.status == "skipped"


def create_legacy_schema_without_migration_version(db_path: Path) -> None:
    now = db_now_utc().isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                work_name TEXT NOT NULL DEFAULT '',
                manga_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE generation_jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                panel_id TEXT NOT NULL,
                candidate_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                current INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                node TEXT,
                message TEXT NOT NULL DEFAULT '生成待ちです',
                candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE knowledge_sources (
                id TEXT PRIMARY KEY,
                work_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                doc_type TEXT NOT NULL DEFAULT 'txt',
                usage TEXT NOT NULL DEFAULT 'reference',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE knowledge_chunks (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                work_name TEXT NOT NULL,
                usage TEXT NOT NULL DEFAULT 'reference',
                kind TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                policy TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        manga = MangaProject(
            title="旧DB",
            pages=[
                Page(
                    page=1,
                    theme="旧",
                    layout_template="single",
                    panels=[Panel(panel_id="p01_01", bbox=(0, 0, 1, 1), shot="")],
                )
            ],
        )
        connection.execute(
            """
            INSERT INTO projects (id, title, work_name, manga_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy", "旧DB", "work", manga.model_dump_json(), now, now),
        )
        connection.execute(
            """
            INSERT INTO knowledge_sources (id, work_name, title, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("source", "work", "資料", now),
        )
        connection.execute(
            """
            INSERT INTO knowledge_chunks (
                id, source_id, work_name, title, content, tags, position
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("chunk", "source", "work", "題名", "本文", "tag", 1),
        )
        for job_id in ["job-a", "job-b"]:
            connection.execute(
                """
                INSERT INTO generation_jobs (
                    id, project_id, panel_id, status, message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, "legacy", "p01_01", "running", "旧ジョブ", now, now),
            )


def test_schema_migration_initializes_new_database(tmp_path: Path) -> None:
    from sqlalchemy import text

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'new.db'}")

    with session_factory() as session:
        rows = session.execute(
            text("SELECT version, name, applied_at FROM schema_migrations")
        ).all()
        assert [(row[0], row[1]) for row in rows] == [(1, "baseline_schema")]
        assert rows[0][2]
        assert "revision" in {
            row[1] for row in session.execute(text("PRAGMA table_info(projects)")).all()
        }


def test_schema_migration_updates_legacy_missing_columns_and_keeps_data(tmp_path: Path) -> None:
    from sqlalchemy import text

    db_path = tmp_path / "legacy.db"
    create_legacy_schema_without_migration_version(db_path)

    session_factory = create_session_factory(f"sqlite:///{db_path}")

    with session_factory() as session:
        project_columns = {
            row[1] for row in session.execute(text("PRAGMA table_info(projects)")).all()
        }
        job_columns = {
            row[1] for row in session.execute(text("PRAGMA table_info(generation_jobs)")).all()
        }
        chunk_columns = {
            row[1] for row in session.execute(text("PRAGMA table_info(knowledge_chunks)")).all()
        }
        assert {"revision", "generation_epoch"} <= project_columns
        assert {"prompt_id", "epoch", "generation_input_hash"} <= job_columns
        assert "meta" in chunk_columns
        project = session.get(ProjectRecord, "legacy")
        assert project is not None
        assert project.title == "旧DB"
        assert project.revision == 0
        assert project.generation_epoch == 0
        chunk_meta = session.execute(
            text("SELECT meta FROM knowledge_chunks WHERE id = 'chunk'")
        ).scalar_one()
        assert chunk_meta == ""


def test_schema_migration_terminates_duplicate_active_jobs_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    db_path = tmp_path / "duplicates.db"
    create_legacy_schema_without_migration_version(db_path)

    first_factory = create_session_factory(f"sqlite:///{db_path}")
    # 再起動時に同じrunnerをもう一度通しても、履歴やindexが重複しない。
    second_factory = create_session_factory(f"sqlite:///{db_path}")

    with second_factory() as session:
        rows = session.execute(
            text("SELECT version, name FROM schema_migrations ORDER BY version")
        ).all()
        assert rows == [(1, "baseline_schema")]
        active_jobs = session.execute(
            text(
                """
                SELECT id, status
                FROM generation_jobs
                WHERE project_id = 'legacy' AND panel_id = 'p01_01'
                ORDER BY id
                """
            )
        ).all()
        assert active_jobs == [("job-a", "running"), ("job-b", "error")]
        indexes = session.execute(text("PRAGMA index_list(generation_jobs)")).all()
        assert sum(row[1] == "ux_generation_jobs_active_panel" for row in indexes) == 1

    with first_factory() as session:
        session.add(
            GenerationJobRecord(
                id="job-c",
                project_id="legacy",
                panel_id="p01_01",
                candidate_count=1,
                status="queued",
                candidate_ids_json="[]",
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_schema_migration_failure_stops_startup_and_records_only_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    db_path = tmp_path / "failure.db"

    def fail_migration(connection) -> None:
        connection.execute(text("CREATE TABLE should_rollback (id TEXT PRIMARY KEY)"))
        raise RuntimeError("boom")

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            database_module.MIGRATIONS[0],
            database_module.SchemaMigration(2, "failing_migration", fail_migration),
        ),
    )

    with pytest.raises(
        database_module.SchemaMigrationError, match="失敗しました。起動を停止します"
    ):
        create_session_factory(f"sqlite:///{db_path}")

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert rows == [(1, "baseline_schema")]
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_rollback'"
            ).fetchone()
            is None
        )


def test_schema_migration_rolls_back_alter_table_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    db_path = tmp_path / "alter-failure.db"
    create_session_factory(f"sqlite:///{db_path}")

    def fail_after_alter(connection) -> None:
        connection.execute(text("ALTER TABLE projects ADD COLUMN should_rollback TEXT"))
        raise RuntimeError("boom")

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            database_module.MIGRATIONS[0],
            database_module.SchemaMigration(2, "failing_alter", fail_after_alter),
        ),
    )
    with pytest.raises(database_module.SchemaMigrationError, match="失敗しました"):
        create_session_factory(f"sqlite:///{db_path}")

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        assert "should_rollback" not in columns
        assert connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1, "baseline_schema")]


def test_schema_migration_concurrent_runners_apply_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from sqlalchemy import text

    db_path = tmp_path / "concurrent-migration.db"
    create_session_factory(f"sqlite:///{db_path}")
    calls = 0
    calls_lock = threading.Lock()

    def migrate_once(connection) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
        connection.execute(text("CREATE TABLE concurrent_once (id TEXT PRIMARY KEY)"))
        time.sleep(0.1)

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            database_module.MIGRATIONS[0],
            database_module.SchemaMigration(2, "concurrent_once", migrate_once),
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        factories = list(
            executor.map(lambda _index: create_session_factory(f"sqlite:///{db_path}"), range(2))
        )
    assert len(factories) == 2
    assert calls == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1, "baseline_schema"), (2, "concurrent_once")]


def test_schema_migration_recreates_referenced_parent_with_foreign_keys_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    db_path = tmp_path / "recreate-parent.db"
    create_session_factory(f"sqlite:///{db_path}")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE migration_parent (id TEXT PRIMARY KEY, label TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE migration_child (
                id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL REFERENCES migration_parent(id)
            )
            """
        )
        connection.execute("INSERT INTO migration_parent VALUES ('parent-1', 'before')")
        connection.execute("INSERT INTO migration_child VALUES ('child-1', 'parent-1')")

    def recreate_parent(connection) -> None:
        connection.execute(
            text(
                """
                CREATE TABLE migration_parent_new (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    migrated INTEGER NOT NULL DEFAULT 1
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO migration_parent_new (id, label)
                SELECT id, label FROM migration_parent
                """
            )
        )
        connection.execute(text("DROP TABLE migration_parent"))
        connection.execute(text("ALTER TABLE migration_parent_new RENAME TO migration_parent"))

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            database_module.MIGRATIONS[0],
            database_module.SchemaMigration(
                2,
                "recreate_parent",
                recreate_parent,
                requires_foreign_keys_off=True,
            ),
        ),
    )
    migrated_factory = create_session_factory(f"sqlite:///{db_path}")

    with migrated_factory() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT id, label, migrated FROM migration_parent"
        ).fetchall() == [("parent-1", "before", 1)]
        assert connection.execute("SELECT id, parent_id FROM migration_child").fetchall() == [
            ("child-1", "parent-1")
        ]
        assert connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1, "baseline_schema"), (2, "recreate_parent")]


def test_schema_migration_unknown_newer_version_stops_startup(tmp_path: Path) -> None:
    db_path = tmp_path / "newer.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (999, "future", db_now_utc().isoformat()),
        )

    with pytest.raises(database_module.SchemaMigrationError, match="新しいDB schema version"):
        create_session_factory(f"sqlite:///{db_path}")
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
            ).fetchone()
            is None
        )


def test_schema_migration_name_mismatch_stops_startup(tmp_path: Path) -> None:
    db_path = tmp_path / "name-mismatch.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (1, "wrong_name", db_now_utc().isoformat()),
        )

    with pytest.raises(database_module.SchemaMigrationError, match="未知のmigration名"):
        create_session_factory(f"sqlite:///{db_path}")


def test_schema_migration_gap_stops_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def noop_migration(_connection) -> None:
        return None

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            database_module.MIGRATIONS[0],
            database_module.SchemaMigration(2, "second", noop_migration),
            database_module.SchemaMigration(3, "third", noop_migration),
        ),
    )
    db_path = tmp_path / "gap.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (1, "baseline_schema", db_now_utc().isoformat()),
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (3, "third", db_now_utc().isoformat()),
        )

    with pytest.raises(database_module.SchemaMigrationError, match="欠番があります"):
        create_session_factory(f"sqlite:///{db_path}")


def test_generation_service_rejects_duplicate_active_panel(tmp_path: Path) -> None:
    from sqlalchemy import text

    from backend.app.generation_service import ActiveJobConflictError, GenerationService

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'enqueue.db'}")
    manga = MangaProject(
        title="t",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="single",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0, 0, 1, 1),
                        shot="",
                        generation=GenerationInfo(status="running"),
                    )
                ],
            )
        ],
    )
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title="t",
                work_name="",
                manga_json=manga.model_dump_json(),
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    service = GenerationService(session_factory, tmp_path / "exports")
    service.enqueue("p", ["p01_01"], 1, "queued")
    with pytest.raises(ActiveJobConflictError):
        service.enqueue("p", ["p01_01"], 1, "duplicate")
    with session_factory() as session:
        assert session.query(GenerationJobRecord).count() == 1
        indexes = session.execute(text("PRAGMA index_list(generation_jobs)")).all()
        assert any(row[1] == "ux_generation_jobs_active_panel" and row[2] == 1 for row in indexes)


def test_generation_service_ignores_and_terminates_old_epoch_active_job(tmp_path: Path) -> None:
    from backend.app.generation_service import GenerationService

    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'old-epoch.db'}")
    manga = MangaProject(
        title="t",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="single",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0, 0, 1, 1),
                        shot="",
                        generation=GenerationInfo(status="running"),
                    )
                ],
            )
        ],
    )
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="p",
                title="t",
                work_name="",
                manga_json=manga.model_dump_json(),
                generation_epoch=1,
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
        session.add(
            GenerationJobRecord(
                id="old",
                project_id="p",
                panel_id="p01_01",
                candidate_count=1,
                status="running",
                epoch=0,
                candidate_ids_json="[]",
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    jobs = GenerationService(session_factory, tmp_path / "exports").enqueue(
        "p", ["p01_01"], 1, "new", expected_epoch=1
    )
    assert jobs[0].epoch == 1
    with session_factory() as session:
        assert session.get(GenerationJobRecord, "old").status == "cancelled"


def test_stale_structural_replace_does_not_cancel_active_job(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        job = app.state.generation.enqueue(project_id, ["p01_01"], 1, "停止されないジョブ")[0]
        app.state.job_manager.register_in_memory(job)
        response = client.post(
            f"/api/projects/{project_id}/generate-name?revision=0",
            json={
                "work_name": "stale",
                "character_a": "A",
                "character_b": "B",
                "situation": "stale",
                "ending_direction": "stale",
            },
        )
        assert response.status_code == 409
        assert app.state.job_manager.get(job.id).status == "queued"


def test_manga_json_structure_change_advances_epoch_and_cancels_old_job(
    tmp_path: Path, monkeypatch
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        job = app.state.generation.enqueue(project_id, ["p01_01"], 1, "旧構成ジョブ")[0]
        app.state.job_manager.register_in_memory(job)
        app.state.job_manager.update(job, status="running", prompt_id="old-prompt")
        update_panel_in_latest(
            app,
            project_id,
            "p01_01",
            lambda panel, page: setattr(panel.generation, "status", "running"),
        )
        stopped: list[str | None] = []
        monkeypatch.setattr(
            generation_module,
            "stop_comfyui_generation",
            lambda settings, prompt_id: stopped.append(prompt_id) or "interrupted",
        )
        detail = client.get(f"/api/projects/{project_id}").json()
        before_epoch = app.state.mutation.current_epoch(project_id)
        manga = detail["manga_json"]
        page = manga["pages"][0]
        page["panels"][0], page["panels"][1] = page["panels"][1], page["panels"][0]
        page["reading_order"] = [panel["panel_id"] for panel in page["panels"]]
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert response.status_code == 200
        assert app.state.mutation.current_epoch(project_id) == before_epoch + 1
        assert app.state.job_manager.get(job.id).status == "cancelled"
        assert stopped == ["old-prompt"]
        updated_panel = next(
            panel
            for page in response.json()["project"]["manga_json"]["pages"]
            for panel in page["panels"]
            if panel["panel_id"] == "p01_01"
        )
        assert updated_panel["generation"]["status"] == "pending"
        retried = client.post(
            mutation_url(client, project_id, "panels/p01_01/generation-jobs"),
            json={"candidate_count": 1},
        )
        assert retried.status_code == 200
        new_job = app.state.job_manager.get(retried.json()["result"]["id"])
        assert new_job is not None
        assert new_job.epoch == before_epoch + 1


def test_structure_change_cancels_db_only_job_for_deleted_panel(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        detail = client.get(f"/api/projects/{project_id}").json()
        epoch = app.state.mutation.current_epoch(project_id)
        timestamp = db_now_utc()
        job_id = "db-only-deleted-panel"
        with app.state.SessionLocal() as session:
            session.add(
                GenerationJobRecord(
                    id=job_id,
                    project_id=project_id,
                    panel_id="p01_01",
                    candidate_count=1,
                    status="running",
                    progress=10,
                    message="メモリ未登録の旧ジョブ",
                    epoch=epoch,
                    candidate_ids_json="[]",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            session.commit()

        manga = detail["manga_json"]
        page = manga["pages"][0]
        page["panels"] = [panel for panel in page["panels"] if panel["panel_id"] != "p01_01"]
        page["reading_order"] = [
            panel_id for panel_id in page["reading_order"] if panel_id != "p01_01"
        ]
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert response.status_code == 200

        with app.state.SessionLocal() as session:
            persisted = session.get(GenerationJobRecord, job_id)
            assert persisted is not None
            assert persisted.status == "cancelled"
            assert persisted.prompt_id is None
            assert persisted.message == "作品構成の更新により前の生成を中断しました"


def test_structure_change_clears_stale_active_job_id_on_done_panel(tmp_path: Path) -> None:
    """生成中に候補採用でdoneへ移ったpanelの古いactive_job_idが構造置換で外れることを確認する。"""
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app

        def set_done_with_stale_owner(panel, page) -> None:
            panel.generation.status = "done"
            panel.generation.active_job_id = "ghost-job"
            panel.generation.prompt_id = "ghost-prompt"

        update_panel_in_latest(app, project_id, "p01_01", set_done_with_stale_owner)

        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        page = manga["pages"][0]
        page["panels"][0], page["panels"][1] = page["panels"][1], page["panels"][0]
        page["reading_order"] = [panel["panel_id"] for panel in page["panels"]]
        response = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert response.status_code == 200
        panel = next(
            panel
            for page in response.json()["project"]["manga_json"]["pages"]
            for panel in page["panels"]
            if panel["panel_id"] == "p01_01"
        )
        # statusに関係なく所有権を外す。doneは中断扱いにはしない。
        assert panel["generation"]["active_job_id"] is None
        assert panel["generation"]["prompt_id"] is None
        assert panel["generation"]["status"] == "done"


def test_legacy_done_page_without_render_asset_migrates_to_pending(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        with app.state.SessionLocal() as session:
            record = session.get(ProjectRecord, project_id)
            manga = json.loads(record.manga_json)
            manga["pages"][0]["render_status"] = "done"
            manga["pages"][0]["rendered_at"] = db_now_utc().isoformat()
            manga["pages"][0].pop("render_asset", None)
            manga["pages"][0].pop("render_hash", None)
            record.manga_json = json.dumps(manga, ensure_ascii=False)
            session.commit()
        page = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert page["render_status"] == "pending"
        assert page["render_asset"] is None
        assert page["render_hash"] is None


def test_structural_replace_discards_stale_job_candidate(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)

        class EpochBumpingBackend:
            def __init__(self) -> None:
                self.done = False

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                # 生成中にネーム再生成・ストーリー適用相当で世代が進んだ状況を再現する。
                if not self.done:
                    self.done = True
                    with app.state.SessionLocal() as session:
                        record = session.get(ProjectRecord, project_id_)
                        record.generation_epoch += 1
                        session.commit()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (1, 2, 3)).save(target_path)
                return ImageResult("stub", "done", target_path, "ok")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: EpochBumpingBackend()
        )

        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        # 世代が変わってジョブがcancelledになると、同期生成APIは409を返す。
        assert response.status_code == 409
        detail = client.get(f"/api/projects/{project_id}").json()
        panel = next(
            p for p in detail["manga_json"]["pages"][0]["panels"] if p["panel_id"] == "p01_01"
        )
        # 世代が変わったため、古いプロンプトの候補は新作品へ混入しない。
        assert panel["image_candidates"] == []
        assert panel["selected_candidate_id"] is None
        jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        assert jobs[0]["status"] == "cancelled"
        # 破棄した候補PNG本体も残らない（current/history未参照のため回収）。
        panels_dir = tmp_path / "exports" / project_id / "panels"
        assert not list(panels_dir.rglob("*.png")) if panels_dir.exists() else True


def test_render_aborts_when_epoch_changes_midway(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)

        class EpochBumpingBackend:
            def __init__(self) -> None:
                self.count = 0

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                self.count += 1
                # 1件目の生成中に構成全置換(ネーム再生成・並べ替え)で世代を進める。
                if self.count == 1:
                    with app.state.SessionLocal() as session:
                        record = session.get(ProjectRecord, project_id_)
                        record.generation_epoch += 1
                        session.commit()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (1, 2, 3)).save(target_path)
                return ImageResult("stub", "done", target_path, "ok")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: EpochBumpingBackend()
        )

        # 開始時epochで固定された/renderは、途中で世代が変わると409で止まる。
        response = client.post(mutation_url(client, project_id, "render"), json={"force": True})
        assert response.status_code == 409
        # 新しい作品構成へ後続ジョブを積まない（最初の1件だけで中止する）。
        jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "cancelled"
        # 構成置換後のページをdoneへ確定しない。
        pages = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"]
        assert all(page["render_status"] == "pending" for page in pages)


def test_completed_job_not_reverted_by_stale_job_stop(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)
        # 新jobを正常完了させる（完了時にactive_job_id=Noneへ戻る）。
        assert (
            client.post(
                mutation_url(client, project_id, "panels/p01_01/generate-image"),
                json={"candidate_count": 1},
            ).status_code
            == 200
        )
        panel = next(
            p
            for p in client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "panels"
            ]
            if p["panel_id"] == "p01_01"
        )
        assert panel["generation"]["status"] == "done"
        selected_before = panel["selected_candidate_id"]
        candidates_before = len(panel["image_candidates"])

        # 遅れて到着した旧jobの停止処理は、完了済みpanelを上書きしない。
        epoch = app.state.mutation.current_epoch(project_id)
        stale_job = GenerationJob(
            project_id=project_id, panel_id="p01_01", candidate_count=1, epoch=epoch
        )
        mark_panel_job_stopped(app, stale_job, "生成をキャンセルしました")

        after = next(
            p
            for p in client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "panels"
            ]
            if p["panel_id"] == "p01_01"
        )
        assert after["generation"]["status"] == "done"
        assert after["selected_candidate_id"] == selected_before
        assert len(after["image_candidates"]) == candidates_before


def test_missing_selected_asset_blocks_render_and_cbz(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        # 全ページ生成・レンダリングして完成状態にする。
        assert client.post(mutation_url(client, project_id, "render")).status_code == 200
        assert client.get(f"/api/projects/{project_id}/production-status").json()["status"] == (
            "complete"
        )
        # 採用candidateは残したまま、採用画像ファイルだけを消す非構造的な欠損を作る。
        detail = client.get(f"/api/projects/{project_id}").json()
        asset = detail["manga_json"]["pages"][0]["panels"][0]["image_asset"]
        assert asset
        (tmp_path / "exports" / asset).unlink()

        # production statusはcompleteにならず、欠損をblocker化する。
        status = client.get(f"/api/projects/{project_id}/production-status").json()
        assert status["status"] != "complete"
        assert any("欠損" in blocker for blocker in status["blockers"])

        # 採用画像が欠損したコマがあると、CBZ出力は422で拒否する。
        export = client.post(mutation_url(client, project_id, "export/cbz"))
        assert export.status_code == 422
        assert not list((tmp_path / "exports" / project_id).glob("*.cbz"))

        # /renderもプレースホルダを完成PNGとして確定せず409で中止する。
        render = client.post(mutation_url(client, project_id, "render"))
        assert render.status_code == 409


def test_missing_selected_asset_blocks_single_page_render(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        assert client.post(mutation_url(client, project_id, "render")).status_code == 200
        detail = client.get(f"/api/projects/{project_id}").json()
        page = detail["manga_json"]["pages"][0]
        asset = page["panels"][0]["image_asset"]
        assert asset
        (tmp_path / "exports" / asset).unlink()
        before_render_asset = page["render_asset"]

        # 直接ページrender・コマ単位render-pageも採用asset欠損なら409で確定しない。
        page_render = client.post(mutation_url(client, project_id, "pages/1/render"))
        assert page_render.status_code == 409
        panel_render = client.post(
            mutation_url(client, project_id, f"panels/{page['panels'][0]['panel_id']}/render-page")
        )
        assert panel_render.status_code == 409
        # render_asset/render_statusが新規確定されていない（古い参照のまま）。
        page_after = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert page_after["render_asset"] == before_render_asset


def test_long_prompt_does_not_break_stale_candidate_cleanup(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)
        # 500文字超のpromptを仕込む（cleanupが任意文字列をパス扱いするとOSErrorになる）。
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        long_prompt = "a" * 600
        manga["pages"][0]["panels"][0]["prompt"] = long_prompt
        manga["pages"][0]["panels"][0]["generation"]["prompt"] = long_prompt
        assert put_manga(client, project_id, manga).status_code == 200

        class EditingBackend:
            def __init__(self) -> None:
                self.count = 0

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                self.count += 1
                if self.count == 2:

                    def change_input(target_panel, target_page) -> None:
                        target_panel.prompt = "DIFFERENT"
                        target_panel.generation.prompt = "DIFFERENT"

                    update_panel_in_latest(app, project_id_, "p01_01", change_input)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (4, 5, 6)).save(target_path)
                return ImageResult("stub", "done", target_path, "ok")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: EditingBackend()
        )
        # 長promptでも、入力不一致の破棄が502ではなく本来の409で完結する。
        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 2},
        )
        assert response.status_code == 409
        jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        assert jobs[0]["status"] == "cancelled"
        # 破棄候補のPNGは残らない。
        panels_dir = tmp_path / "exports" / project_id / "panels"
        assert not list(panels_dir.rglob("*.png")) if panels_dir.exists() else True


def test_relative_export_dir_cleanup_keeps_referenced_asset(tmp_path: Path, monkeypatch) -> None:
    # 相対EXPORT_DIRでも、参照中assetを誤って削除しないこと（パス基準の統一）。
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        export_dir=Path("exports"),
        image_backend="stub",
    )
    with TestClient(create_app(settings)) as client:
        app = client.app
        project_id = create_generated_project(client)
        keep_rel = f"{project_id}/references/keep.png"
        keep_path = tmp_path / "exports" / keep_rel
        keep_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(keep_path)
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        manga["characters"][0]["reference_image_asset"] = keep_rel
        assert put_manga(client, project_id, manga).status_code == 200

        # ownershipへ相対パスで参照中assetを入れてcleanupしても、参照中なので削除されない。
        app.state.rendering.cleanup_published_assets(project_id, {Path("exports") / keep_rel: True})
        assert keep_path.is_file()


def test_cancelled_job_cleans_up_returned_candidate_png(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)

        class CancelledThenReturnBackend:
            def __init__(self) -> None:
                self.cancelled_once = False

            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                # 1回目だけ、結果返却直前にjobがキャンセル済みになった状況を再現する
                # （backendがCancelledErrorを内部で吸収して遅延PNGを返すケース）。
                if not self.cancelled_once:
                    self.cancelled_once = True
                    manager = app.state.job_manager
                    for job in list(manager.jobs.values()):
                        if (
                            job.project_id == project_id_
                            and job.panel_id == "p01_01"
                            and job.status == "running"
                        ):
                            # 実際のキャンセル同様、DBへもcancelledを永続化する。
                            manager.update(job, status="cancelled")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (7, 8, 9)).save(target_path)
                return ImageResult("stub", "done", target_path, "ok")

        shared_backend = CancelledThenReturnBackend()
        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: shared_backend
        )
        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        # キャンセル済みjobの同期生成は409。返ってきたPNGは孤児化させず回収する。
        assert response.status_code == 409
        panels_dir = tmp_path / "exports" / project_id / "panels"
        assert not list(panels_dir.rglob("*.png")) if panels_dir.exists() else True
        # panelはrunningのまま固定されず、skippedへ確定する。
        panel = next(
            p
            for p in client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "panels"
            ]
            if p["panel_id"] == "p01_01"
        )
        assert panel["generation"]["status"] == "skipped"
        # 直後の再生成が受理される（runningのままだと409で再生成不能になる）。
        retry = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        assert retry.status_code == 200


def test_backend_exception_after_png_write_cleans_up_orphan(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)

        class WriteThenFailBackend:
            async def generate_panel(
                self,
                project_id_,
                panel_,
                export_dir,
                target_path=None,
                progress_callback=None,
                on_prompt_id=None,
            ):
                # PNGを書いた後に例外化する（502になるが孤児PNGを残さないこと）。
                target_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (3, 3, 3)).save(target_path)
                raise RuntimeError("backend爆発")

        monkeypatch.setattr(
            generation_module, "build_image_backend", lambda settings: WriteThenFailBackend()
        )
        response = client.post(
            mutation_url(client, project_id, "panels/p01_01/generate-image"),
            json={"candidate_count": 1},
        )
        assert response.status_code == 502
        panels_dir = tmp_path / "exports" / project_id / "panels"
        assert not list(panels_dir.rglob("*.png")) if panels_dir.exists() else True
        panel = next(
            p
            for p in client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0][
                "panels"
            ]
            if p["panel_id"] == "p01_01"
        )
        assert panel["generation"]["status"] == "error"


def test_character_profiles_are_composed_without_duplicates() -> None:
    manga = MangaProject.model_validate(
        {
            "title": "prompt合成",
            "common_positive_prompt": "masterpiece, anime style",
            "common_negative_prompt": "low quality, text",
            "characters": [
                {
                    "id": "hero",
                    "display_name": "主人公",
                    "trigger_prompt": "hero trigger",
                    "appearance_prompt": "black hair, blue eyes",
                    "outfit_prompt": "school uniform",
                    "negative_prompt": "different hairstyle, text",
                }
            ],
            "pages": [
                {
                    "page": 1,
                    "theme": "test",
                    "layout_template": "one",
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0, 0, 1, 1],
                            "shot": "顔アップ",
                            "characters": ["hero"],
                            "generation": {
                                "prompt": "anime style, smiling",
                                "negative_prompt": "bad hands, text",
                            },
                        }
                    ],
                }
            ],
        }
    )
    positive, negative = compose_panel_prompts(manga, manga.pages[0].panels[0])
    assert (
        positive
        == "masterpiece, anime style, hero trigger, black hair, blue eyes, school uniform, smiling"
    )
    assert negative == "low quality, text, different hairstyle, bad hands"


def test_prompt_preview_endpoint_uses_character_profiles(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.get(f"/api/projects/{project_id}/panels/p01_01/prompt-preview")
        assert response.status_code == 200
        payload = response.json()
        assert payload["character_ids"] == ["char_a", "char_b"]
        assert "春香" in payload["positive_prompt"]
        assert "千早" in payload["positive_prompt"]
        assert "inconsistent character design" in payload["negative_prompt"]


def test_character_adapters_are_prepared_for_panel() -> None:
    manga = MangaProject.model_validate(
        {
            "title": "adapter",
            "characters": [
                {
                    "id": "hero",
                    "display_name": "主人公",
                    "lora_node_id": "20",
                    "lora_name": "hero.safetensors",
                    "reference_image_asset": "exports/project/references/hero.png",
                    "reference_load_node_id": "30",
                }
            ],
            "pages": [
                {
                    "page": 1,
                    "theme": "test",
                    "layout_template": "one",
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0, 0, 1, 1],
                            "shot": "test",
                            "characters": ["hero"],
                        }
                    ],
                }
            ],
        }
    )
    prepared = prepare_panel_for_generation(manga, manga.pages[0].panels[0])
    assert prepared.generation.loras[0].node_id == "20"
    assert prepared.generation.reference_images[0].node_id == "30"


def test_location_and_control_references_are_prepared() -> None:
    manga = MangaProject.model_validate(
        {
            "title": "scene",
            "active_workflow_preset_id": "anime",
            "workflow_presets": [{"id": "anime", "name": "anime", "steps": 20}],
            "locations": [
                {
                    "id": "room",
                    "display_name": "部屋",
                    "prompt": "consistent room",
                    "negative_prompt": "changing room",
                    "reference_image_asset": "exports/project/room.png",
                    "reference_load_node_id": "50",
                }
            ],
            "pages": [
                {
                    "page": 1,
                    "theme": "test",
                    "layout_template": "one",
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0, 0, 1, 1],
                            "shot": "test",
                            "location_id": "room",
                            "control_references": [
                                {
                                    "id": "pose",
                                    "kind": "pose",
                                    "asset": "exports/project/pose.png",
                                    "load_node_id": "51",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    panel = manga.pages[0].panels[0]
    positive, negative = compose_panel_prompts(manga, panel)
    prepared = prepare_panel_for_generation(manga, panel)
    assert "consistent room" in positive
    assert "changing room" in negative
    assert prepared.generation.workflow_preset.id == "anime"
    assert [(item.node_id, item.kind) for item in prepared.generation.reference_images] == [
        ("50", "location"),
        ("51", "pose"),
    ]


def test_reference_image_is_uploaded_and_patched(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    source = export_dir / "project" / "references" / "hero.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "red").save(source)
    workflow = {"30": {"class_type": "LoadImage", "inputs": {"image": "old.png"}}}
    panel = Panel(
        panel_id="p01_01",
        bbox=(0, 0, 1, 1),
        shot="test",
        generation=GenerationInfo(
            reference_images=[{"node_id": "30", "asset": str(source), "character_id": "hero"}]
        ),
    )

    class UploadClient:
        async def post(self, url: str, files: dict, data: dict) -> httpx.Response:
            return response(
                url, {"name": "hero.png", "subfolder": data["subfolder"], "type": "input"}
            )

    asyncio.run(
        apply_reference_images_to_workflow(
            UploadClient(), "http://comfy", workflow, panel, export_dir, "project"
        )
    )
    assert workflow["30"]["inputs"]["image"] == "local-doujin-studio/project/references/hero.png"


def test_reference_image_relative_asset_id_is_resolved_against_export_dir(tmp_path: Path) -> None:
    # 参照画像は相対アセットID(project/...形式)で保存される。CWDではなくexport_dir基準で解決すること。
    export_dir = tmp_path / "exports"
    source = export_dir / "project" / "references" / "hero.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(source)
    workflow = {"30": {"class_type": "LoadImage", "inputs": {"image": "old.png"}}}
    panel = Panel(
        panel_id="p01_01",
        bbox=(0, 0, 1, 1),
        shot="test",
        generation=GenerationInfo(
            reference_images=[{"node_id": "30", "asset": "project/references/hero.png"}]
        ),
    )

    class UploadClient:
        async def post(self, url: str, files: dict, data: dict) -> httpx.Response:
            return response(
                url, {"name": "hero.png", "subfolder": data["subfolder"], "type": "input"}
            )

    asyncio.run(
        apply_reference_images_to_workflow(
            UploadClient(), "http://comfy", workflow, panel, export_dir, "project"
        )
    )
    assert workflow["30"]["inputs"]["image"] == "local-doujin-studio/project/references/hero.png"


def test_reference_image_outside_export_dir_is_rejected(tmp_path: Path) -> None:
    # export_dir外を指す参照IDは見つからない扱いにする（フォールバックさせる）。
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    panel = Panel(
        panel_id="p01_01",
        bbox=(0, 0, 1, 1),
        shot="test",
        generation=GenerationInfo(reference_images=[{"node_id": "30", "asset": "../secret.png"}]),
    )
    workflow = {"30": {"class_type": "LoadImage", "inputs": {"image": "old.png"}}}

    class UploadClient:
        async def post(
            self, url: str, files: dict, data: dict
        ) -> httpx.Response:  # pragma: no cover
            raise AssertionError("export_dir外の参照はアップロードしてはいけない")

    try:
        asyncio.run(
            apply_reference_images_to_workflow(
                UploadClient(), "http://comfy", workflow, panel, export_dir, "project"
            )
        )
        raise AssertionError("ValueErrorが送出されるべき")
    except ValueError as exc:
        assert "参照画像が見つかりません" in str(exc)


def test_reference_image_upload_api(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        image = Image.new("RGB", (20, 20), "blue")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        response = client.post(
            mutation_url(client, project_id, "characters/char_a/reference-image"),
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        asset = response.json()["result"]["asset"]
        assert (tmp_path / "exports" / asset).exists()
        assert (
            response.json()["project"]["manga_json"]["characters"][0]["reference_image_asset"]
            == asset
        )


def test_save_request_image_parallel_same_target(tmp_path: Path, monkeypatch) -> None:
    """同じ参照先への並行アップロードで一時ファイルが衝突せず、両方成功することを確認する。"""
    import threading

    target = tmp_path / "refs" / "char_a.png"

    def make_png(color: str) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
        return buffer.getvalue()

    class FakeRequest:
        def __init__(self, content: bytes) -> None:
            self._content = content

        async def body(self) -> bytes:
            return self._content

    # 両スレッドが一時ファイル書き込みを終えてreplaceへ到達した時点で揃える。
    # replace自体は直列化し、固定一時ファイルなら片方が消えてFileNotFoundErrorになる状況を再現する。
    barrier = threading.Barrier(2)
    replace_lock = threading.Lock()
    original_replace = Path.replace

    def synced_replace(self: Path, dst):
        barrier.wait(timeout=5)
        with replace_lock:
            return original_replace(self, dst)

    monkeypatch.setattr(Path, "replace", synced_replace)

    errors: list[Exception] = []

    def worker(content: bytes) -> None:
        try:
            asyncio.run(router_common.save_request_image(FakeRequest(content), target))
        except Exception as exc:  # noqa: BLE001 - テストで全例外を収集する
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(make_png(color),)) for color in ("red", "blue")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"並行アップロードが失敗しました: {errors}"
    assert target.exists()
    with Image.open(target) as saved:
        assert saved.size == (8, 8)
    # 一時ファイルが残らないこと。
    assert list(target.parent.glob("*.tmp")) == []


def test_reference_upload_conflict_keeps_content_addressed_asset(
    tmp_path: Path, monkeypatch
) -> None:
    """PNG保存後・JSON反映前に競合(409)しても、内容hash不変assetは削除しない。

    削除すると、同一内容を並行uploadしたcreated=Falseの後続リクエストがcommit直前に
    targetを失い、成功応答なのにJSONが欠損assetを参照する不整合になる。失敗側は
    targetを残し（無害な内容キャッシュ）、JSONは未参照のままにする。
    """
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app
        stale_revision = current_revision(client, project_id)

        captured: dict[str, Path] = {}
        original = router_common.save_content_addressed_request_image

        async def inject_conflict(request, asset_dir, asset_kind, *, preserve_alpha=False):
            target, created = await original(
                request, asset_dir, asset_kind, preserve_alpha=preserve_alpha
            )
            captured["target"] = target

            # 保存直後に別経路でmanga_jsonを変更しrevisionを進め、楽観ロック競合(409)を起こす。
            def bump(manga: MangaProject) -> None:
                manga.premise = (manga.premise or "") + "x"

            app.state.mutation.mutate_local(project_id, bump)
            return target, created

        monkeypatch.setattr(
            projects_router, "save_content_addressed_request_image", inject_conflict
        )

        buffer = io.BytesIO()
        Image.new("RGB", (12, 12), "red").save(buffer, format="PNG")
        response = client.post(
            f"/api/projects/{project_id}/characters/char_a/reference-image?revision={stale_revision}",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        # revision不一致で409になること。
        assert response.status_code == 409
        # 失敗してもtargetは削除されない（並行する後続リクエストの参照を壊さないため）。
        assert "target" in captured
        assert captured["target"].exists()
        # 失敗リクエスト自身のJSONはassetを参照しないこと。
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        asset_id = assets_module.path_to_asset_id(captured["target"], app.state.settings.export_dir)
        assert manga["characters"][0]["reference_image_asset"] != asset_id
        # 一時ファイルも残らないこと。
        assert list((tmp_path / "exports").rglob("*.tmp")) == []


def test_same_content_concurrent_publish_exactly_one_created(tmp_path: Path, monkeypatch) -> None:
    """同一内容を同じ参照先へ並行公開しても、created=Trueは原子的に1回だけになる。

    両スレッドが一時ファイル書き込み後・publish前で揃うため、exists()事前確認方式なら
    両者がcreated=Trueになって失敗側cleanupが成功側assetを消しうる状況を再現する。
    """
    import threading

    asset_dir = tmp_path / "refs"
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(buffer, format="PNG")
    png = buffer.getvalue()

    class FakeRequest:
        def __init__(self, content: bytes) -> None:
            self._content = content

        async def body(self) -> bytes:
            return self._content

    original_save = router_common.save_request_image
    barrier = threading.Barrier(2)

    async def synced_save(request, target, preserve_alpha=False):
        await original_save(request, target, preserve_alpha=preserve_alpha)
        # 両スレッドがtemp書き込みを終え、publish直前で揃える。
        barrier.wait(timeout=5)

    monkeypatch.setattr(router_common, "save_request_image", synced_save)

    results: list[tuple[Path, bool]] = []
    lock = threading.Lock()

    def worker() -> None:
        target, created = asyncio.run(
            router_common.save_content_addressed_request_image(
                FakeRequest(png), asset_dir, "character"
            )
        )
        with lock:
            results.append((target, created))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    targets = {target for target, _ in results}
    assert len(targets) == 1, "同一内容は同一targetへ公開される"
    # 原子的判定なら初公開だけTrue。exists()方式だと両方Trueになりこのassertで落ちる。
    assert sorted(created for _, created in results) == [False, True]
    assert next(iter(targets)).exists()
    assert [item for item in asset_dir.iterdir() if item.name.startswith(".")] == []


def test_same_content_parallel_upload_keeps_winner_asset(tmp_path: Path) -> None:
    """同一内容を同じ参照先へ並行upload相当で送り、片方409でも成功側assetを消さない。"""
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        buffer = io.BytesIO()
        Image.new("RGB", (14, 14), "blue").save(buffer, format="PNG")
        png = buffer.getvalue()

        winner_revision = current_revision(client, project_id)
        # 先行リクエスト: 成功してassetをJSONへ紐付ける。
        first = client.post(
            f"/api/projects/{project_id}/characters/char_a/reference-image?revision={winner_revision}",
            content=png,
            headers={"Content-Type": "image/png"},
        )
        assert first.status_code == 200
        asset = first.json()["result"]["asset"]
        assert (tmp_path / "exports" / asset).is_file()

        # 後発リクエスト: 同一内容・同一参照先だがrevisionが古く409。
        # 後発のpublishはcreated=Falseとなり、cleanupは成功側assetを消さない。
        second = client.post(
            f"/api/projects/{project_id}/characters/char_a/reference-image?revision={winner_revision}",
            content=png,
            headers={"Content-Type": "image/png"},
        )
        assert second.status_code == 409

        # 成功側のJSONがassetを参照し続け、ファイルも残る。
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        assert manga["characters"][0]["reference_image_asset"] == asset
        assert (tmp_path / "exports" / asset).is_file()
        # 孤児.tmpが残らないこと。
        assert list((tmp_path / "exports").rglob("*.tmp")) == []


@pytest.mark.parametrize("left,right", [("春香", "千早"), ("a/b", "a:b")])
def test_reference_upload_keeps_colliding_ids_separate(
    tmp_path: Path, left: str, right: str
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        manga["characters"][0]["id"] = left
        manga["characters"][1]["id"] = right
        assert put_manga(client, project_id, manga).status_code == 200

        assets: list[str] = []
        for character_id, color in ((left, "red"), (right, "blue")):
            image = Image.new("RGB", (12, 12), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            response = client.post(
                mutation_url(
                    client,
                    project_id,
                    f"characters/{quote(character_id, safe='')}/reference-image",
                ),
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            assets.append(response.json()["result"]["asset"])

        assert assets[0] != assets[1]
        colors = []
        for asset in assets:
            with Image.open(io.BytesIO(client.get(f"/api/assets/{asset}").content)) as saved:
                colors.append(saved.getpixel((0, 0)))
        assert colors == [(255, 0, 0), (0, 0, 255)]


@pytest.mark.parametrize("left,right", [("事務所", "公園"), ("a/b", "a:b")])
def test_location_upload_keeps_colliding_ids_separate(
    tmp_path: Path, left: str, right: str
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        template = manga["locations"][0]
        manga["locations"] = [
            {**template, "id": left, "display_name": left},
            {**template, "id": right, "display_name": right},
        ]
        assert put_manga(client, project_id, manga).status_code == 200

        assets: list[str] = []
        for location_id, color in ((left, "red"), (right, "blue")):
            image = Image.new("RGB", (12, 12), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            response = client.post(
                mutation_url(
                    client,
                    project_id,
                    f"locations/{quote(location_id, safe='')}/reference-image",
                ),
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            assets.append(response.json()["result"]["asset"])

        assert assets[0] != assets[1]
        assert all((tmp_path / "exports" / asset).is_file() for asset in assets)


def test_asset_api_rejects_path_traversal(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/assets/../test.db")
        assert response.status_code == 404


def test_overlay_asset_upload_and_project_preflight(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        page = manga["pages"][0]
        page["overlay_elements"] = [
            {
                "id": "hero unsafe",
                "source_panel_id": page["panels"][0]["panel_id"],
                "box": [0.2, 0.2, 0.4, 0.5],
            }
        ]
        assert put_manga(client, project_id, manga).status_code == 200

        image = Image.new("RGBA", (32, 32), (255, 0, 0, 128))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        response = client.post(
            f"/api/projects/{project_id}/pages/1/overlays/hero%20unsafe/asset"
            f"?revision={current_revision(client, project_id)}",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        asset = response.json()["result"]["asset"]
        assert " " not in asset
        assert (tmp_path / "exports" / asset).is_file()

        manga = response.json()["project"]["manga_json"]
        manga["pages"][0]["reading_order"] = ["unknown-panel"]
        assert put_manga(client, project_id, manga).status_code == 200
        preflight_response = client.post(f"/api/projects/{project_id}/preflight")
        assert preflight_response.status_code == 200
        assert any(
            issue["code"] == "invalid_reading_order"
            for issue in preflight_response.json()["errors"]
        )


@pytest.mark.parametrize("left,right", [("春香", "千早"), ("a/b", "a:b")])
def test_overlay_upload_keeps_colliding_ids_separate(tmp_path: Path, left: str, right: str) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        panel_id = manga["pages"][0]["panels"][0]["panel_id"]
        manga["pages"][0]["overlay_elements"] = [
            {"id": overlay_id, "source_panel_id": panel_id, "box": [0.2, 0.2, 0.4, 0.5]}
            for overlay_id in (left, right)
        ]
        assert put_manga(client, project_id, manga).status_code == 200

        assets: list[str] = []
        for overlay_id, color in ((left, "red"), (right, "blue")):
            image = Image.new("RGBA", (12, 12), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            response = client.post(
                f"/api/projects/{project_id}/pages/1/overlays/{quote(overlay_id, safe='')}/asset"
                f"?revision={current_revision(client, project_id)}",
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            assets.append(response.json()["result"]["asset"])

        assert assets[0] != assets[1]
        assert all((tmp_path / "exports" / asset).is_file() for asset in assets)


def test_batch_generation_queue_completes_page(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(
            mutation_url(client, project_id, "generation-jobs"),
            json={"page": 1, "candidate_count": 1},
        )
        assert response.status_code == 200
        jobs = response.json()["result"]["jobs"]
        assert len(jobs) == 4
        for job in jobs:
            with client.websocket_connect(f"/api/generation-jobs/{job['id']}/ws") as websocket:
                while True:
                    state = websocket.receive_json()
                    if state["status"] in {"done", "error", "cancelled"}:
                        break
            assert state["status"] == "done"
        status = client.get(f"/api/projects/{project_id}/production-status").json()
        assert status["pages"][0]["status"] == "ready"
        assert status["pages"][0]["adopted_panels"] == 4
        history = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        assert len(history) == 4
        assert all(job["status"] == "done" for job in history)


def test_job_manager_restores_running_job_from_database(tmp_path: Path) -> None:
    session_factory = create_session_factory(f"sqlite:///{tmp_path / 'jobs.db'}")
    # generation_jobs.project_idはprojectsへの外部キーなので親レコードを先に作る。
    with session_factory() as session:
        session.add(
            ProjectRecord(
                id="project",
                title="t",
                work_name="",
                manga_json="{}",
                created_at=db_now_utc(),
                updated_at=db_now_utc(),
            )
        )
        session.commit()
    manager = JobManager(session_factory)
    # running(ComfyUI投入済みかもしれない) と queued(未開始) を1つずつ用意する。
    running_job = manager.create("project", "panel-a", 2)
    manager.update(running_job, status="running", progress=45, message="生成中", prompt_id="pid")
    queued_job = manager.create("project", "panel-b", 1)

    restored_manager = JobManager(session_factory)
    to_start, interrupted = restored_manager.restore_pending()
    # 未開始のqueuedジョブだけ再開対象になる。
    assert [job.id for job in to_start] == [queued_job.id]
    assert to_start[0].status == "queued"
    # runningだったジョブは二重投入を避けるためerror(要再実行)へ。再開はしない。
    assert [job.id for job in interrupted] == [running_job.id]
    recovered_running = restored_manager.get(running_job.id)
    assert recovered_running.status == "error"
    assert "再起動により中断" in recovered_running.message
    assert recovered_running.prompt_id is None


def test_generate_sixteen_page_name(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/projects",
            json={"title": "16ページ本", "work_name": "作品", "target_pages": 16},
        )
        project_id = response.json()["project"]["id"]
        response = client.post(
            f"/api/projects/{project_id}/generate-name?revision=0",
            json={
                "work_name": "作品",
                "character_a": "A",
                "character_b": "B",
                "situation": "部屋で相談する",
                "ending_direction": "笑って終わる",
                "target_pages": 16,
            },
        )
        assert response.status_code == 200
        manga = response.json()["project"]["manga_json"]
        assert manga["target_pages"] == 16
        assert len(manga["pages"]) == 16
        panel_ids = [panel["panel_id"] for page in manga["pages"] for panel in page["panels"]]
        assert len(panel_ids) == len(set(panel_ids))


def test_location_and_control_image_upload_apis(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        image = Image.new("RGB", (20, 20), "green")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        response = client.post(
            mutation_url(client, project_id, "locations/default_room/reference-image"),
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        response = client.post(
            mutation_url(
                client,
                project_id,
                "panels/p01_01/controls/pose/reference-image?load_node_id=51",
            ),
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        panel = response.json()["project"]["manga_json"]["pages"][0]["panels"][0]
        assert panel["control_references"][0]["kind"] == "pose"
        assert panel["control_references"][0]["load_node_id"] == "51"


def test_fit_image_cover_and_contain_modes() -> None:
    source = Image.new("RGB", (100, 50), (255, 0, 0))
    cover = fit_image_to_box(source, (40, 40), "cover", "center")
    contain = fit_image_to_box(source, (40, 40), "contain", "center")
    assert cover.size == (40, 40)
    assert contain.size == (40, 40)
    assert contain.getpixel((20, 2)) == (245, 245, 242)


def test_long_dialogue_renders_with_auto_font_shrink(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/projects", json={"title": "写植テスト", "work_name": ""})
        project_id = response.json()["project"]["id"]
        manga = MangaProject(
            title="写植テスト",
            target_pages=4,
            pages=[
                Page(
                    page=1,
                    theme="長文",
                    layout_template="one",
                    panels=[
                        Panel(
                            panel_id="p01_01",
                            bbox=(0.05, 0.05, 0.9, 0.4),
                            shot="テスト",
                            dialogue=[
                                Dialogue(
                                    speaker="char_a",
                                    text="これはかなり長い台詞なので吹き出しの中に収まるように自動で小さくなる必要があります。",
                                    box=(0.05, 0.05, 0.5, 0.22),
                                    font_size=36,
                                    max_lines=3,
                                )
                            ],
                        )
                    ],
                )
            ],
        )
        response = put_manga(client, project_id, manga.model_dump())
        assert response.status_code == 200
        response = client.post(mutation_url(client, project_id, "panels/p01_01/render-page"))
        assert response.status_code == 200


def sample_workflow() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "positive"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "negative"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
    }


def workflow_config(tmp_path: Path) -> ComfyUIWorkflowConfig:
    return ComfyUIWorkflowConfig(
        workflow_path=tmp_path / "workflow_api.json",
        positive_node_id="6",
        negative_node_id="7",
        seed_node_id="3",
        width_node_id="5",
        height_node_id="5",
        save_prefix_node_id="9",
    )


class MockComfyUIClient:
    async def __aenter__(self) -> "MockComfyUIClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def post(self, url: str, json: dict) -> httpx.Response:
        assert url.endswith("/prompt")
        assert json["prompt"]["6"]["inputs"]["text"]
        return response(url, {"prompt_id": "prompt-1"})

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        if url.endswith("/system_stats"):
            return response(url, {"system": "ok"})
        if url.endswith("/history/prompt-1"):
            return response(
                url,
                {
                    "prompt-1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "panel.png", "subfolder": "", "type": "output"},
                                ]
                            }
                        }
                    }
                },
            )
        if url.endswith("/view"):
            image = Image.new("RGB", (32, 32), (120, 140, 160))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return httpx.Response(200, content=buffer.getvalue(), request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))


def response(url: str, payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def test_delete_project_removes_database_records_and_assets(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/projects", json={"title": "削除対象", "work_name": "作品", "target_pages": 4}
        )
        assert created.status_code == 200
        project_id = created.json()["project"]["id"]
        project_dir = tmp_path / "exports" / project_id
        project_dir.mkdir(parents=True)
        (project_dir / "generated.png").write_bytes(b"asset")

        deleted = client.delete(f"/api/projects/{project_id}")

        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/api/projects/{project_id}").status_code == 404
        assert not project_dir.exists()


def test_polygon_panel_is_clipped_when_rendered(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/projects", json={"title": "変形コマ", "work_name": "作品", "target_pages": 4}
        ).json()
        project_id = created["project"]["id"]
        manga = MangaProject(
            title="変形コマ",
            work_name="作品",
            premise="",
            target_pages=4,
            pages=[
                Page(
                    page=1,
                    theme="",
                    layout_template="polygon",
                    panels=[
                        Panel(
                            panel_id="p01_01",
                            bbox=(0.1, 0.1, 0.8, 0.8),
                            shape_points=[(0.2, 0), (1, 0), (0.8, 1), (0, 1)],
                            shot="変形",
                        )
                    ],
                )
            ],
        )
        assert put_manga(client, project_id, manga.model_dump()).status_code == 200
        rendered = client.post(mutation_url(client, project_id, "panels/p01_01/render-page")).json()
        asset = rendered["result"]["page_asset"]
        with Image.open(tmp_path / "exports" / asset) as image:
            # 左上は傾斜で切り抜かれ、中央はコマのplaceholder色になる。
            assert image.getpixel((125, 175)) == (248, 248, 244)
            assert image.getpixel((600, 850))[:3] == (230, 232, 235)
