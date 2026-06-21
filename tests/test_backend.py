from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.app.main as main_module
from backend.app.config import Settings
from backend.app.database import ProjectRecord, create_session_factory
from backend.app.database import now_utc as db_now_utc
from backend.app.generator import DEFAULT_COMMON_NEGATIVE_PROMPT, DEFAULT_COMMON_POSITIVE_PROMPT
from backend.app.image_backends import (
    ComfyUIWorkflowConfig,
    ImageResult,
    apply_panel_to_workflow,
    apply_reference_images_to_workflow,
)
from backend.app.jobs import JobManager
from backend.app.main import create_app
from backend.app.prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from backend.app.renderer import fit_image_to_box, sanitize_export_filename
from backend.app.schemas import Dialogue, GenerationInfo, MangaProject, Page, Panel


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


def create_generated_project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"title": "テスト本", "work_name": "テスト作品"})
    assert response.status_code == 200
    project_id = response.json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/generate-name",
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
        response = client.post(f"/api/projects/{project_id}/render")
        assert response.status_code == 200
        assets = response.json()["page_assets"]
        assert len(assets) == 4
        for asset in assets:
            asset_response = client.get(f"/api/assets/{asset}")
            assert asset_response.status_code == 200

        response = client.post(f"/api/projects/{project_id}/export/cbz")
        assert response.status_code == 200
        cbz_asset = response.json()["cbz_asset"]
        cbz_path = tmp_path / "exports" / cbz_asset
        assert cbz_path.exists()
        assert cbz_path.name.startswith("テスト本-")
        assert cbz_path.name.endswith(".cbz")
        assert Path(response.json()["absolute_path"]) == cbz_path.resolve()
        with ZipFile(cbz_path) as archive:
            assert archive.namelist() == [
                "page_001.png",
                "page_002.png",
                "page_003.png",
                "page_004.png",
            ]
        assert response.json()["warnings"] == []
        status = client.get(f"/api/projects/{project_id}/production-status").json()
        assert status["status"] == "complete"
        assert status["adopted_panels"] == status["total_panels"]


def test_open_export_folder_selects_cbz(tmp_path: Path, monkeypatch) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(main_module, "open_in_file_manager", lambda path: opened.append(path))
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        client.post(f"/api/projects/{project_id}/export/cbz")
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
        response = client.post(f"/api/projects/{project_id}/render")
        assert response.status_code == 200
        manga = response.json()["manga_json"]
        first_panel = manga["pages"][0]["panels"][0]
        assert first_panel["generation"]["backend"] == "comfyui"
        assert first_panel["generation"]["status"] == "fallback"
        assert first_panel["image_asset"]


def test_comfyui_missing_workflow_is_error_not_silent_stub(tmp_path: Path) -> None:
    # 設定不備（ワークフロー欠如）は黙ってstubへ退避せず、エラーとして表面化させる。
    with make_client(tmp_path, image_backend="comfyui") as client:
        project_id = create_generated_project(client)
        response = client.post(f"/api/projects/{project_id}/render")
        assert response.status_code == 200
        first_panel = response.json()["manga_json"]["pages"][0]["panels"][0]
        assert first_panel["generation"]["status"] == "error"
        assert first_panel["image_asset"] is None


def test_invalid_manga_json_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/projects", json={"title": "不正テスト", "work_name": ""})
        project_id = response.json()["id"]
        response = client.put(
            f"/api/projects/{project_id}/manga-json",
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
        assert first.json()["revision"] == revision + 1

        # 古いrevisionでの保存は409で弾く（生成完了・別タブ保存との競合）。
        stale = client.put(f"/api/projects/{project_id}/manga-json?revision={revision}", json=manga)
        assert stale.status_code == 409

        # revision未指定なら従来通り保存できる。
        no_check = client.put(f"/api/projects/{project_id}/manga-json", json=manga)
        assert no_check.status_code == 200


def test_generation_merge_preserves_concurrent_panel_edit(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        app = client.app

        # ユーザーがp01_02のプロンプトを編集して保存する。
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        for panel in manga["pages"][0]["panels"]:
            if panel["panel_id"] == "p01_02":
                panel["prompt"] = "ユーザー編集後のプロンプト"
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200

        # 生成完了が開始時点の古いスナップショットではなく最新へp01_01だけマージする。
        main_module.update_panel_in_latest(
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
        response = client.put(f"/api/projects/{project_id}/manga-json", json=manga)
        assert response.status_code == 422


def test_metadata_change_keeps_rendered_pages_but_render_change_invalidates(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        assert client.post(f"/api/projects/{project_id}/render").status_code == 200
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        assert all(page["render_status"] == "done" for page in manga["pages"])

        # メタ変更（タイトル）だけでは全ページをpendingに戻さない。
        manga["title"] = "新しいタイトル"
        meta_only = client.put(f"/api/projects/{project_id}/manga-json", json=manga)
        assert meta_only.status_code == 200
        assert all(
            page["render_status"] == "done" for page in meta_only.json()["manga_json"]["pages"]
        )

        # 描画に影響する変更（1ページ目の台詞追加）は当該ページだけpendingにする。
        manga2 = meta_only.json()["manga_json"]
        manga2["pages"][0]["panels"][0]["dialogue"].append(
            {"speaker": "char_a", "text": "追加の台詞"}
        )
        render_change = client.put(f"/api/projects/{project_id}/manga-json", json=manga2)
        assert render_change.status_code == 200
        pages = render_change.json()["manga_json"]["pages"]
        assert pages[0]["render_status"] == "pending"
        assert all(page["render_status"] == "done" for page in pages[1:])


def test_duplicate_workflow_preset_id_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        preset = manga["workflow_presets"][0]
        manga["workflow_presets"] = [preset, {**preset, "name": "重複ID"}]
        response = client.put(f"/api/projects/{project_id}/manga-json", json=manga)
        assert response.status_code == 422


def test_generation_keeps_user_selection_made_during_run(tmp_path: Path, monkeypatch) -> None:
    with make_client(tmp_path) as client:
        app = client.app
        project_id = create_generated_project(client)
        # 既存候補を2つ作る（2回目が選択状態になる）。
        client.post(f"/api/projects/{project_id}/panels/p01_01/use-stub")
        client.post(f"/api/projects/{project_id}/panels/p01_01/use-stub")
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
                    main_module.update_panel_in_latest(
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

        monkeypatch.setattr(main_module, "build_image_backend", lambda settings: SelectingBackend())

        response = client.post(
            f"/api/projects/{project_id}/panels/p01_01/generate-image",
            json={"candidate_count": 1},
        )
        assert response.status_code == 200
        panel2 = next(
            p
            for p in response.json()["manga_json"]["pages"][0]["panels"]
            if p["panel_id"] == "p01_01"
        )
        # 新候補は追加されるが、生成中のユーザー選択は自動採用で上書きされない。
        assert len(panel2["image_candidates"]) == 3
        assert panel2["selected_candidate_id"] == user_pick


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
        main_module.httpx, "Client", lambda *a, **k: _queue_client([], ["pid"], posted)
    )
    assert main_module.stop_comfyui_generation(settings, "pid") == "queued_removed"
    assert any(url.endswith("/queue") and json == {"delete": ["pid"]} for url, json in posted)
    assert not any(url.endswith("/interrupt") for url, _ in posted)


def test_stop_comfyui_interrupts_only_when_running(monkeypatch) -> None:
    settings = Settings(image_backend="comfyui")
    posted: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        main_module.httpx, "Client", lambda *a, **k: _queue_client(["pid"], [], posted)
    )
    assert main_module.stop_comfyui_generation(settings, "pid") == "interrupted"
    assert any(url.endswith("/interrupt") for url, _ in posted)


def test_stop_comfyui_skips_unrelated_generation(monkeypatch) -> None:
    settings = Settings(image_backend="comfyui")
    posted: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        main_module.httpx, "Client", lambda *a, **k: _queue_client(["other"], ["another"], posted)
    )
    # 対象prompt_idがキューに無ければグローバルinterruptもqueue削除もしない。
    assert main_module.stop_comfyui_generation(settings, "pid") == "not_requested"
    assert posted == []


def test_cancel_completed_job_is_noop(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        # generate-imageはジョブ完了まで待つため、終了後にキャンセルする。
        generated = client.post(
            f"/api/projects/{project_id}/panels/p01_01/generate-image",
            json={"candidate_count": 1},
        )
        assert generated.status_code == 200
        jobs = client.get(f"/api/projects/{project_id}/generation-jobs").json()
        job = jobs[0]
        assert job["status"] == "done"

        cancelled = client.post(f"/api/generation-jobs/{job['id']}/cancel")
        assert cancelled.status_code == 200
        # 完了済みジョブのキャンセルは何もしない（doneのまま）。
        assert cancelled.json()["status"] == "done"
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


def test_use_stub_creates_unique_candidate_files(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        client.post(f"/api/projects/{project_id}/panels/p01_01/use-stub")
        client.post(f"/api/projects/{project_id}/panels/p01_01/use-stub")
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
        assert client.post(f"/api/projects/{project_id}/pages/1/render").status_code == 200
        page1 = client.get(f"/api/projects/{project_id}").json()["manga_json"]["pages"][0]
        assert page1["render_status"] == "done"

        # ページ1のコマで候補を生成→自動採用されると、当該ページはpendingへ戻る。
        client.post(
            f"/api/projects/{project_id}/panels/p01_01/generate-image",
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

        response = client.post(f"/api/projects/{project_id}/panels/p01_01/generate-image")
        assert response.status_code == 200
        first_panel = response.json()["manga_json"]["pages"][0]["panels"][0]
        assert first_panel["generation"]["status"] == "done"
        assert first_panel["generation"]["prompt_id"] == "prompt-1"
        assert (tmp_path / "exports" / first_panel["image_asset"]).exists()


def test_single_panel_stub_endpoint_updates_panel(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(f"/api/projects/{project_id}/panels/p01_01/use-stub")
        assert response.status_code == 200
        first_panel = response.json()["manga_json"]["pages"][0]["panels"][0]
        assert first_panel["generation"]["backend"] == "stub"
        assert first_panel["generation"]["status"] == "done"
        assert first_panel["image_asset"]


def test_single_panel_render_page_endpoint_updates_page_png(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(f"/api/projects/{project_id}/panels/p01_01/use-stub")
        assert response.status_code == 200
        response = client.post(f"/api/projects/{project_id}/panels/p01_01/render-page")
        assert response.status_code == 200
        page_asset = response.json()["page_asset"]
        assert page_asset == f"{project_id}/pages/page_001.png"
        assert (tmp_path / "exports" / page_asset).exists()


def test_generation_job_creates_candidates_and_selects_one(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(
            f"/api/projects/{project_id}/panels/p01_01/generation-jobs",
            json={"candidate_count": 2},
        )
        assert response.status_code == 200
        job_id = response.json()["id"]

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
            f"/api/projects/{project_id}/panels/p01_01/candidates/{first_candidate_id}/select"
        )
        assert response.status_code == 200
        selected_panel = response.json()["manga_json"]["pages"][0]["panels"][0]
        assert selected_panel["selected_candidate_id"] == first_candidate_id
        assert response.json()["page_asset"] == f"{project_id}/pages/page_001.png"


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
            f"/api/projects/{project_id}/characters/char_a/reference-image",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        asset = response.json()["asset"]
        assert (tmp_path / "exports" / asset).exists()
        assert response.json()["manga_json"]["characters"][0]["reference_image_asset"] == asset


@pytest.mark.parametrize("left,right", [("春香", "千早"), ("a/b", "a:b")])
def test_reference_upload_keeps_colliding_ids_separate(
    tmp_path: Path, left: str, right: str
) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        manga["characters"][0]["id"] = left
        manga["characters"][1]["id"] = right
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200

        assets: list[str] = []
        for character_id, color in ((left, "red"), (right, "blue")):
            image = Image.new("RGB", (12, 12), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            response = client.post(
                f"/api/projects/{project_id}/characters/{quote(character_id, safe='')}/reference-image",
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            assets.append(response.json()["asset"])

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
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200

        assets: list[str] = []
        for location_id, color in ((left, "red"), (right, "blue")):
            image = Image.new("RGB", (12, 12), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            response = client.post(
                f"/api/projects/{project_id}/locations/{quote(location_id, safe='')}/reference-image",
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            assets.append(response.json()["asset"])

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
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200

        image = Image.new("RGBA", (32, 32), (255, 0, 0, 128))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        response = client.post(
            f"/api/projects/{project_id}/pages/1/overlays/hero%20unsafe/asset",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        asset = response.json()["asset"]
        assert " " not in asset
        assert (tmp_path / "exports" / asset).is_file()

        manga = response.json()["manga_json"]
        manga["pages"][0]["reading_order"] = ["unknown-panel"]
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200
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
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200

        assets: list[str] = []
        for overlay_id, color in ((left, "red"), (right, "blue")):
            image = Image.new("RGBA", (12, 12), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            response = client.post(
                f"/api/projects/{project_id}/pages/1/overlays/{quote(overlay_id, safe='')}/asset",
                content=buffer.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            assets.append(response.json()["asset"])

        assert assets[0] != assets[1]
        assert all((tmp_path / "exports" / asset).is_file() for asset in assets)


def test_batch_generation_queue_completes_page(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        response = client.post(
            f"/api/projects/{project_id}/generation-jobs",
            json={"page": 1, "candidate_count": 1},
        )
        assert response.status_code == 200
        jobs = response.json()["jobs"]
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
    restored = restored_manager.restore_pending()
    # 未開始のqueuedジョブだけ再開対象になる。
    assert [job.id for job in restored] == [queued_job.id]
    assert restored[0].status == "queued"
    # runningだったジョブは二重投入を避けるためerror(要再実行)へ。再開はしない。
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
        project_id = response.json()["id"]
        response = client.post(
            f"/api/projects/{project_id}/generate-name",
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
        manga = response.json()["manga_json"]
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
            f"/api/projects/{project_id}/locations/default_room/reference-image",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        response = client.post(
            f"/api/projects/{project_id}/panels/p01_01/controls/pose/reference-image?load_node_id=51",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        panel = response.json()["manga_json"]["pages"][0]["panels"][0]
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
        project_id = response.json()["id"]
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
        response = client.put(f"/api/projects/{project_id}/manga-json", json=manga.model_dump())
        assert response.status_code == 200
        response = client.post(f"/api/projects/{project_id}/panels/p01_01/render-page")
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
