from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.config import Settings
from backend.app.main import create_app


def make_stub_client(
    tmp_path: Path,
    *,
    image_backend: str = "stub",
    workflow_path: Path | None = None,
    knowledge_dir: Path | None = None,
    llm_provider: str = "stub",
    database_name: str = "test.db",
) -> TestClient:
    """stub構成のFastAPIテストクライアントを作成する。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / database_name}",
        export_dir=tmp_path / "exports",
        knowledge_dir=knowledge_dir or tmp_path / "knowledge",
        image_backend=image_backend,
        llm_provider=llm_provider,
        comfyui_base_url="http://127.0.0.1:9",
        comfyui_workflow_path=workflow_path or tmp_path / "missing.workflow_api.json",
    )
    return TestClient(create_app(settings))


def create_stub_project(
    client: TestClient,
    *,
    title: str = "本",
    work_name: str = "作品",
    target_pages: int = 4,
    character_a: str = "春香",
    character_b: str = "千早",
    situation: str = "事務所で相談する",
    ending_direction: str = "笑って終わる",
) -> str:
    """generate-name済みの標準テストプロジェクトを作成する。"""
    response = client.post(
        "/api/projects",
        json={"title": title, "work_name": work_name, "target_pages": target_pages},
    )
    assert response.status_code == 200
    project_id = response.json()["project"]["id"]
    response = client.post(
        f"/api/projects/{project_id}/generate-name?revision=0",
        json={
            "work_name": work_name,
            "character_a": character_a,
            "character_b": character_b,
            "situation": situation,
            "ending_direction": ending_direction,
            "target_pages": target_pages,
        },
    )
    assert response.status_code == 200
    return project_id


def latest_revision(client: TestClient, project_id: str) -> int:
    """プロジェクトの最新revisionを返す。"""
    return client.get(f"/api/projects/{project_id}").json()["revision"]


def mutation_url(client: TestClient, project_id: str, suffix: str) -> str:
    """最新revision付きのmutation URLを作る。"""
    separator = "&" if "?" in suffix else "?"
    return f"/api/projects/{project_id}/{suffix}{separator}revision={latest_revision(client, project_id)}"


def make_png_bytes(color: str, *, mode: str = "RGB", size: tuple[int, int] = (10, 10)) -> bytes:
    """指定色のPNGバイト列を作成する。"""
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()
