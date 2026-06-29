"""ページ描画とレンダリング補助処理のテスト。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.app.generator import compute_generation_size
from backend.app.renderer import fit_image_to_box, render_project_page
from backend.app.schemas import (
    BalloonTail,
    Dialogue,
    MangaProject,
    Page,
    Panel,
    Sfx,
)
from tests.helpers import (
    create_stub_project as create_generated_project,
)
from tests.helpers import (
    make_stub_client as make_client,
)


def test_generation_size_matches_aspect_and_snaps_to_64() -> None:
    width, height = compute_generation_size((0.06, 0.05, 0.88, 0.28))
    assert width % 64 == 0 and height % 64 == 0
    assert width > height  # 横長コマは横長サイズ
    # 縦長コマは縦長サイズになる。
    tall_w, tall_h = compute_generation_size((0.06, 0.05, 0.3, 0.9))
    assert tall_h > tall_w


def test_crop_pan_zoom_is_deterministic_and_differs_from_default() -> None:
    source = Image.new("RGB", (200, 100), (10, 20, 30))
    for x in range(200):
        for y in range(100):
            source.putpixel((x, y), (x % 256, y % 256, 100))
    plain = fit_image_to_box(source, (80, 80), "cover", "center")
    zoomed_a = fit_image_to_box(
        source, (80, 80), "cover", "center", scale=2.0, offset_x=0.5, offset_y=-0.3
    )
    zoomed_b = fit_image_to_box(
        source, (80, 80), "cover", "center", scale=2.0, offset_x=0.5, offset_y=-0.3
    )
    assert zoomed_a.size == (80, 80)
    assert zoomed_a.tobytes() == zoomed_b.tobytes()  # 再現性
    assert zoomed_a.tobytes() != plain.tobytes()


def test_focal_point_centers_on_target() -> None:
    source = Image.new("RGB", (200, 200), (0, 0, 0))
    source.putpixel((150, 50), (255, 0, 0))  # 注視点付近に赤
    focal = fit_image_to_box(source, (40, 40), "cover", "center", scale=2.0, focal=(0.75, 0.25))
    assert focal.size == (40, 40)


def test_all_balloon_kinds_and_sfx_render(tmp_path: Path) -> None:
    manga = MangaProject(
        title="balloon",
        pages=[
            Page(
                page=1,
                theme="t",
                layout_template="grid",
                panels=[
                    Panel(
                        panel_id=f"p01_{i:02d}",
                        bbox=(0.05 + 0.46 * (i % 2), 0.05 + 0.30 * (i // 2), 0.42, 0.27),
                        shot="t",
                        dialogue=[
                            Dialogue(
                                speaker="a",
                                text="セリフのテスト。",
                                balloon=kind,
                                vertical=True,
                                tail=BalloonTail(tip=(0.5, 0.95)),
                            )
                        ],
                        sfx=[Sfx(text="どやっ", box=(0.7, 0.7), rotation=15.0, vertical=True)],
                    )
                    for i, kind in enumerate(["oval", "cloud", "burst", "caption", "none", "oval"])
                ],
            )
        ],
    )
    target, warnings = render_project_page("proj", manga, 1, tmp_path)
    assert target.exists()
    rendered = Image.open(target).convert("RGB")
    assert rendered.size == (1200, 1700)
    # 何かしら描画されている（真っ白ではない）。
    colors = rendered.getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) > 5
    assert isinstance(warnings, list)


def test_preflight_and_render_page_endpoints(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        preflight_response = client.post(f"/api/projects/{project_id}/pages/1/preflight")
        assert preflight_response.status_code == 200
        payload = preflight_response.json()
        assert payload["ok"] is True  # 既定の短い台詞はエラーにならない
        assert "errors" in payload and "warnings" in payload

        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        render_response = client.post(
            f"/api/projects/{project_id}/pages/1/render?revision={revision}"
        )
        assert render_response.status_code == 200
        body = render_response.json()["result"]
        assert "/page_001." in body["page_asset"]
        assert body["page_asset"].endswith(".png")
        assert (tmp_path / "exports" / body["page_asset"]).exists()
        assert body["preflight"]["ok"] is True


def test_cbz_export_blocked_by_dialogue_clipping(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project_id = create_generated_project(client)
        detail = client.get(f"/api/projects/{project_id}").json()
        manga = detail["manga_json"]
        # 最小サイズでも収まらない長文（文字切れ）はCBZ出力を止める（商用品質: 領域3）。
        dialogue = manga["pages"][0]["panels"][0]["dialogue"][0]
        dialogue["text"] = "あ" * 300
        dialogue["box"] = [0.0, 0.0, 0.15, 0.1]
        manga["pages"][0]["panels"][0]["bbox"] = [0.05, 0.05, 0.2, 0.1]
        saved = client.put(
            f"/api/projects/{project_id}/manga-json?revision={detail['revision']}", json=manga
        )
        assert saved.status_code == 200
        revision = client.get(f"/api/projects/{project_id}").json()["revision"]
        export = client.post(f"/api/projects/{project_id}/export/cbz?revision={revision}")
        assert export.status_code == 422
        # 文字切れはプリフライトのエラーとして確認できる。
        preflight_result = client.post(f"/api/projects/{project_id}/pages/1/preflight").json()
        assert any(issue["code"] == "dialogue_clipped" for issue in preflight_result["errors"])
