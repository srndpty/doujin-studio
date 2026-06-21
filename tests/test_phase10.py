"""Phase 3（オーバーフレーム・品質検査）のテスト。"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import layout_engine, preflight
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.renderer import render_project_page
from backend.app.schemas import (
    BalloonTail,
    Dialogue,
    MangaProject,
    OverlayElement,
    Page,
    Panel,
)


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        export_dir=tmp_path / "exports",
        image_backend="stub",
    )
    return TestClient(create_app(settings))


def create_generated_project(client: TestClient) -> str:
    project_id = client.post("/api/projects", json={"title": "本", "work_name": "作品"}).json()[
        "id"
    ]
    client.post(
        f"/api/projects/{project_id}/generate-name",
        json={
            "work_name": "作品",
            "character_a": "春香",
            "character_b": "千早",
            "situation": "事務所で相談する",
            "ending_direction": "笑って終わる",
        },
    )
    return project_id


def _two_panel_page(overlay: OverlayElement | None = None) -> MangaProject:
    return MangaProject(
        title="overlay",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="grid",
                reading_order=["p01_01", "p01_02"],
                overlay_elements=[overlay] if overlay else [],
                panels=[
                    Panel(panel_id="p01_01", bbox=(0.05, 0.05, 0.9, 0.42), shot="t"),
                    Panel(panel_id="p01_02", bbox=(0.05, 0.52, 0.9, 0.42), shot="t"),
                ],
            )
        ],
    )


def test_overlay_renders_with_occlusion_and_placeholder(tmp_path: Path) -> None:
    asset = tmp_path / "overlay.png"
    Image.new("RGBA", (300, 400), (200, 50, 50, 255)).save(asset)
    overlay = OverlayElement(
        id="ov1",
        source_panel_id="p01_02",
        asset=str(asset),
        box=(0.4, 0.3, 0.3, 0.5),
        layer="front",
        occluded_by_panel_ids=["p01_01"],
    )
    manga = _two_panel_page(overlay)
    target, _warnings = render_project_page("proj", manga, 1, tmp_path)
    assert target.exists()

    # アセット未設定でもプレースホルダ枠を描いて落ちない。
    placeholder = OverlayElement(id="ov2", box=(0.4, 0.3, 0.2, 0.2), layer="back")
    manga2 = _two_panel_page(placeholder)
    target2, _ = render_project_page("proj", manga2, 1, tmp_path)
    assert target2.exists()


def test_preflight_flags_dialogue_overflow_as_warning() -> None:
    manga = MangaProject(
        title="overflow",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0.05, 0.05, 0.2, 0.1),
                        shot="t",
                        dialogue=[Dialogue(speaker="a", text="あ" * 300, box=(0.0, 0.0, 0.5, 0.5))],
                    )
                ],
            )
        ],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    # テキストは切り捨てないので重大エラーではなく警告にする。
    overflow = [issue for issue in issues if issue.code == "dialogue_overflow"]
    assert overflow and all(issue.level == "warning" for issue in overflow)


def test_preflight_tail_out_of_range_and_insert_characters() -> None:
    manga = MangaProject(
        title="warn",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="one",
                panels=[
                    Panel(
                        panel_id="p01_01",
                        bbox=(0.05, 0.05, 0.9, 0.9),
                        shot="t",
                        subject_mode="prop_insert",
                        characters=["hero"],
                        dialogue=[
                            Dialogue(speaker="a", text="やあ", tail=BalloonTail(tip=(1.15, 0.5)))
                        ],
                    )
                ],
            )
        ],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "tail_out_of_range" in codes
    assert "insert_panel_has_characters" in codes
    assert all(
        issue.level == "warning"
        for issue in issues
        if issue.code in {"tail_out_of_range", "insert_panel_has_characters"}
    )


def test_preflight_reading_order_reversal_and_overlay_occlusion() -> None:
    overlay = OverlayElement(
        id="ov", source_panel_id="p01_02", box=(0.05, 0.05, 0.9, 0.42), layer="front"
    )
    manga = _two_panel_page(overlay)
    # 読み順を逆転させる。
    manga.pages[0].reading_order = ["p01_02", "p01_01"]
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "reading_order_reversed" in codes
    # overlay(source=p01_02)が先に読むp01_01を隠している（逆順だとp01_02が先なので隠さない）。
    # 自然順に戻すと隠蔽警告が出る。
    manga.pages[0].reading_order = ["p01_01", "p01_02"]
    issues2 = preflight.preflight_page(manga, manga.pages[0])
    assert any(issue.code == "overlay_hides_earlier_panel" for issue in issues2)


def test_preflight_overlay_scale_extends_occlusion() -> None:
    # 右(p01_01)が先、左(p01_02)が後の読み順。overlayはp01_02発で、box単体では
    # 右コマに重ならないが、scaleで右へはみ出して先に読むp01_01を隠す。
    overlay = OverlayElement(
        id="ov", source_panel_id="p01_02", box=(0.45, 0.4, 0.05, 0.1), scale=3.0, layer="front"
    )
    manga = MangaProject(
        title="ov",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="grid",
                reading_order=["p01_01", "p01_02"],
                overlay_elements=[overlay],
                panels=[
                    Panel(panel_id="p01_01", bbox=(0.5, 0.05, 0.45, 0.9), shot="t"),
                    Panel(panel_id="p01_02", bbox=(0.05, 0.05, 0.45, 0.9), shot="t"),
                ],
            )
        ],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    assert any(issue.code == "overlay_hides_earlier_panel" for issue in issues)
    # 倍率1ならはみ出さず検出されない。
    manga.pages[0].overlay_elements[0].scale = 1.0
    issues2 = preflight.preflight_page(manga, manga.pages[0])
    assert not any(issue.code == "overlay_hides_earlier_panel" for issue in issues2)


def test_preflight_three_column_row_has_no_false_gutter_warning() -> None:
    # montageの3分割（1行3コマ）。左右端は間に中央コマがあるので隣接扱いしない。
    boxes = layout_engine.build_page_layout(3, "montage")
    panels = [Panel(panel_id=f"p01_{k + 1:02d}", bbox=boxes[k], shot="t") for k in range(3)]
    manga = MangaProject(
        title="g",
        pages=[Page(page=1, theme="t", layout_template="grid", panels=panels)],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    codes = {issue.code for issue in issues}
    assert "gutter_too_large" not in codes
    assert "panels_overlap" not in codes


def test_preflight_layout_repetition() -> None:
    pages = []
    for page_number in range(1, 4):
        pages.append(
            Page(
                page=page_number,
                theme="t",
                layout_template="grid",
                layout_family="dialogue",
                panels=[
                    Panel(panel_id=f"p{page_number:02d}_01", bbox=(0.05, 0.05, 0.9, 0.9), shot="t")
                ],
            )
        )
    manga = MangaProject(title="rep", pages=pages)
    issues = preflight.preflight_page(manga, manga.pages[2], page_index=2)
    assert any(issue.code == "layout_repetition" for issue in issues)


def test_preflight_and_render_page_endpoints(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        preflight_response = client.post(f"/api/projects/{project_id}/pages/1/preflight")
        assert preflight_response.status_code == 200
        payload = preflight_response.json()
        assert payload["ok"] is True  # 既定の短い台詞はエラーにならない
        assert "errors" in payload and "warnings" in payload

        render_response = client.post(f"/api/projects/{project_id}/pages/1/render")
        assert render_response.status_code == 200
        body = render_response.json()
        assert body["page_asset"].endswith("page_001.png")
        assert (tmp_path / "exports" / body["page_asset"]).exists()
        assert body["preflight"]["ok"] is True


def test_overlay_asset_upload_preserves_alpha(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
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
        assert client.put(f"/api/projects/{project_id}/manga-json", json=manga).status_code == 200

        # 透過PNG（人物切り抜きを想定）をアップロード。
        buffer = io.BytesIO()
        Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(buffer, format="PNG")
        response = client.post(
            f"/api/projects/{project_id}/pages/1/overlays/ov1/asset",
            content=buffer.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        asset = response.json()["asset"]
        saved = Image.open(tmp_path / "exports" / asset)
        # アルファチャンネルが保持され、透明部分が潰れていないこと。
        assert saved.mode == "RGBA"
        assert saved.getpixel((0, 0))[3] == 0


def test_preflight_with_body_checks_posted_manga_without_saving(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        # ページ1をレンダリングしてrender_status=doneにする。
        assert client.post(f"/api/projects/{project_id}/pages/1/render").status_code == 200
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        # 本文側にだけ収まらない長文を仕込む（DBは変更しない）。
        dialogue = manga["pages"][0]["panels"][0]["dialogue"][0]
        dialogue["text"] = "あ" * 300
        dialogue["box"] = [0.0, 0.0, 0.12, 0.08]
        manga["pages"][0]["panels"][0]["bbox"] = [0.05, 0.05, 0.18, 0.1]

        response = client.post(f"/api/projects/{project_id}/pages/1/preflight", json=manga)
        assert response.status_code == 200
        codes = {issue["code"] for issue in response.json()["warnings"]}
        assert "dialogue_overflow" in codes  # 本文の内容が検査される

        # 非破壊: DBは変わらず、render_statusはdoneのまま、台詞も元のまま。
        after = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        assert after["pages"][0]["render_status"] == "done"
        assert after["pages"][0]["panels"][0]["dialogue"][0]["text"] != "あ" * 300


def test_cbz_export_succeeds_despite_dialogue_overflow(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        manga = client.get(f"/api/projects/{project_id}").json()["manga_json"]
        # 収まりにくい長文を仕込んでも、切り捨てない方針なので出力は止めない。
        dialogue = manga["pages"][0]["panels"][0]["dialogue"][0]
        dialogue["text"] = "あ" * 300
        dialogue["box"] = [0.0, 0.0, 0.15, 0.1]
        manga["pages"][0]["panels"][0]["bbox"] = [0.05, 0.05, 0.2, 0.1]
        saved = client.put(f"/api/projects/{project_id}/manga-json", json=manga)
        assert saved.status_code == 200
        export = client.post(f"/api/projects/{project_id}/export/cbz")
        assert export.status_code == 200
        # 窮屈さはプリフライト警告として確認できる。
        preflight_result = client.post(f"/api/projects/{project_id}/pages/1/preflight").json()
        assert any(issue["code"] == "dialogue_overflow" for issue in preflight_result["warnings"])
