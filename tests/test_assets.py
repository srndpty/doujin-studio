import io
from pathlib import Path

import pytest
from conftest import (
    create_stub_project as create_generated_project,
)
from conftest import (
    create_stub_project as create_project,
)
from conftest import (
    latest_revision as revision,
)
from conftest import (
    make_png_bytes as png_bytes,
)
from conftest import (
    make_stub_client as make_client,
)
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

import backend.app.asset_storage as asset_storage_module
import backend.app.generation_service as generation_module
from backend.app.assets import (
    path_to_asset_id,
    resolve_asset_path,
    safe_component,
    stable_asset_name,
)
from backend.app.database import ProjectRevisionRecord, now_utc
from backend.app.prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from backend.app.schemas import MangaProject


@given(st.text(min_size=1, max_size=200))
def test_safe_component_never_creates_path(value: str) -> None:
    result = safe_component(value)
    assert result
    assert "/" not in result
    assert "\\" not in result
    assert result not in {".", ".."}


def test_asset_id_round_trip(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    target = export_dir / "project" / "panels" / "one.png"
    target.parent.mkdir(parents=True)
    target.touch()
    asset_id = path_to_asset_id(target, export_dir)
    assert asset_id == "project/panels/one.png"
    assert resolve_asset_path(asset_id, export_dir) == target.resolve()


def test_resolve_asset_path_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_asset_path("../outside.png", tmp_path / "exports")


@pytest.mark.parametrize("left,right", [("春香", "千早"), ("a/b", "a:b")])
def test_stable_asset_name_avoids_slug_collisions(left: str, right: str) -> None:
    assert stable_asset_name(left, "character") != stable_asset_name(right, "character")


# --- asset参照・アップロードAPI ---


def test_prop_insert_excludes_character_identity() -> None:
    manga = MangaProject.model_validate(
        {
            "title": "t",
            "common_negative_prompt": "low quality",
            "characters": [
                {
                    "id": "hero",
                    "display_name": "主人公",
                    "trigger_prompt": "hero trigger",
                    "appearance_prompt": "black hair",
                    "lora_node_id": "20",
                    "lora_name": "hero.safetensors",
                }
            ],
            "pages": [
                {
                    "page": 1,
                    "theme": "t",
                    "layout_template": "one",
                    "panels": [
                        {
                            "panel_id": "p01_01",
                            "bbox": [0, 0, 1, 1],
                            "shot": "小物",
                            "subject_mode": "prop_insert",
                            "characters": ["hero"],
                            "generation": {"prompt": "a cup of coffee"},
                        }
                    ],
                }
            ],
        }
    )
    panel = manga.pages[0].panels[0]
    positive, negative = compose_panel_prompts(manga, panel)
    assert "hero trigger" not in positive
    assert "black hair" not in positive
    assert "character print on product" in negative
    prepared = prepare_panel_for_generation(manga, panel)
    assert prepared.generation.loras == []


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


def test_overlay_asset_upload_preserves_alpha(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        manga["pages"][0]["overlay_elements"] = [
            {
                "id": "ov1",
                "source_panel_id": "",
                "asset": None,
                "mask_asset": None,
                "box": [0.2, 0.2, 0.4, 0.4],
                "scale": 1.0,
                "opacity": 1.0,
                "layer": "front",
                "z_index": 0,
                "occluded_by_panel_ids": [],
            }
        ]
        assert (
            client.put(
                f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
            ).status_code
            == 200
        )

        # 透過PNG（人物切り抜きを想定）をアップロード。
        buffer = io.BytesIO()
        Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(buffer, format="PNG")
        overlay_revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        response = client.post(
            f"/api/projects/{project_id}/pages/1/overlays/ov1/asset?revision={overlay_revision}",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        asset = response.json()["result"]["asset"]
        saved = Image.open(tmp_path / "exports" / asset)
        # アルファチャンネルが保持され、透明部分が潰れていないこと。
        assert saved.mode == "RGBA"
        assert saved.getpixel((0, 0))[3] == 0
