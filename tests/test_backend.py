from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app


def make_client(tmp_path: Path, image_backend: str = "stub") -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        export_dir=tmp_path / "exports",
        image_backend=image_backend,
        comfyui_base_url="http://127.0.0.1:9",
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
