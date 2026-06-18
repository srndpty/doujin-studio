from __future__ import annotations

import io
import json
from pathlib import Path
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.config import Settings
from backend.app.image_backends import ComfyUIWorkflowConfig, apply_panel_to_workflow
from backend.app.main import create_app
from backend.app.renderer import fit_image_to_box
from backend.app.schemas import Dialogue, GenerationInfo, MangaProject, Page, Panel


def make_client(tmp_path: Path, image_backend: str = "stub", workflow_path: Path | None = None) -> TestClient:
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
        with ZipFile(cbz_path) as archive:
            assert archive.namelist() == ["page_001.png", "page_002.png", "page_003.png", "page_004.png"]


def test_comfyui_unavailable_falls_back_to_stub(tmp_path: Path) -> None:
    with make_client(tmp_path, image_backend="comfyui") as client:
        project_id = create_generated_project(client)
        response = client.post(f"/api/projects/{project_id}/render")
        assert response.status_code == 200
        manga = response.json()["manga_json"]
        first_panel = manga["pages"][0]["panels"][0]
        assert first_panel["generation"]["backend"] == "comfyui"
        assert first_panel["generation"]["status"] == "fallback"
        assert first_panel["image_asset"]


def test_invalid_manga_json_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/projects", json={"title": "不正テスト", "work_name": ""})
        project_id = response.json()["id"]
        response = client.put(
            f"/api/projects/{project_id}/manga-json",
            json={"title": "不正", "target_pages": 4, "pages": [{"page": 1, "theme": "x", "layout_template": "x", "panels": []}]},
        )
        assert response.status_code == 422


def test_workflow_json_is_patched_for_panel(tmp_path: Path) -> None:
    workflow = sample_workflow()
    config = workflow_config(tmp_path)
    panel = Panel(
        panel_id="p01_01",
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="テスト",
        prompt="元prompt",
        generation=GenerationInfo(prompt="差し替えprompt", negative_prompt="bad", seed=42, width=960, height=540),
    )
    patched = apply_panel_to_workflow(workflow, config, panel, "prefix/test")
    assert patched["6"]["inputs"]["text"] == "差し替えprompt, no text, no speech bubble, no watermark, no manga panel text"
    assert patched["7"]["inputs"]["text"] == "bad"
    assert patched["3"]["inputs"]["seed"] == 42
    assert patched["5"]["inputs"]["width"] == 960
    assert patched["5"]["inputs"]["height"] == 540
    assert patched["9"]["inputs"]["filename_prefix"] == "prefix/test"


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
    assert panel.dialogue[0].font_size == 24
    assert panel.dialogue[0].box is None


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
    monkeypatch.setattr("backend.app.image_backends.httpx.AsyncClient", lambda *args, **kwargs: MockComfyUIClient())

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
        assert (tmp_path / first_panel["image_asset"]).exists()


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
