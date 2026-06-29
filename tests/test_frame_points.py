"""frame_points（ページ座標ポリゴンのコマ枠）モデルと描画のテスト。"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from backend.app.renderer import (
    PAGE_SIZE,
    _panel_box_px,
    draw_panel_art,
    panel_frame_points,
    panel_frame_points_px,
    render_project_page,
)
from backend.app.schemas import Dialogue, MangaProject, Page, Panel


def _panel(**kwargs) -> Panel:
    base = {"panel_id": "p", "bbox": (0.1, 0.1, 0.6, 0.6), "shot": "medium"}
    base.update(kwargs)
    return Panel(**base)


def test_frame_points_default_derives_rectangle_from_bbox() -> None:
    panel = _panel(bbox=(0.1, 0.2, 0.4, 0.5))
    assert panel.frame_points is None
    assert panel_frame_points(panel) == [(0.1, 0.2), (0.5, 0.2), (0.5, 0.7), (0.1, 0.7)]
    assert panel.z_index == 0
    assert panel.frame_role == "normal"


def test_frame_points_derive_uses_shape_points_in_page_coords() -> None:
    panel = _panel(bbox=(0.2, 0.2, 0.4, 0.4), shape_points=[(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)])
    result = panel_frame_points(panel)
    expected = [(0.2, 0.2), (0.6, 0.2), (0.4, 0.6)]
    assert panel.shape_points is None
    assert panel.frame_points is not None
    flat = [coord for point in result for coord in point]
    assert flat == pytest.approx([coord for point in expected for coord in point])


def test_shape_points_migration_preserves_bbox() -> None:
    """旧shape_points移行ではbboxを縮めない。bbox相対の子要素（吹き出し・crop・安全領域）
    のページ座標が移動しないようにするため（領域3）。"""
    # bbox内側だけを使う多角形（端に触れない）。旧仕様ならbboxがこの外接矩形へ縮む。
    inner_poly = [(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)]
    panel = _panel(bbox=(0.1, 0.1, 0.6, 0.6), shape_points=inner_poly)
    # bboxは元のまま。frame_pointsはページ座標へ変換される。
    assert panel.bbox == (0.1, 0.1, 0.6, 0.6)
    assert panel.shape_points is None
    assert panel.frame_points is not None
    # _panel_box_px（bbox基準）は移行前と同じ領域を指す。
    box_before = _panel_box_px(_panel(bbox=(0.1, 0.1, 0.6, 0.6)))
    assert _panel_box_px(panel) == box_before


def test_frame_points_canonical_when_set() -> None:
    poly = [(0.0, 0.0), (1.05, 0.0), (1.05, 1.05), (0.0, 1.0)]
    panel = _panel(frame_points=poly)
    assert panel_frame_points(panel) == [(x, y) for x, y in poly]


def test_frame_points_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _panel(frame_points=[(0.0, 0.0), (1.5, 0.0), (1.0, 1.0)])


def test_frame_points_rejects_self_intersection() -> None:
    with pytest.raises(ValidationError):
        _panel(frame_points=[(0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (0.0, 1.0)])


def test_frame_points_px_maps_to_page_pixels() -> None:
    panel = _panel(frame_points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    page_w, page_h = PAGE_SIZE
    assert panel_frame_points_px(panel) == [
        (0, 0),
        (page_w, 0),
        (page_w, page_h),
        (0, page_h),
    ]


def test_z_index_ordering_and_polygon_masking(tmp_path) -> None:
    page = Image.new("RGBA", PAGE_SIZE, (255, 255, 255, 255))
    draw = ImageDraw.Draw(page)
    blue = tmp_path / "blue.png"
    Image.new("RGB", (64, 64), (0, 0, 255)).save(blue)
    red = tmp_path / "red.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(red)
    bottom = _panel(
        panel_id="b",
        bbox=(0.1, 0.1, 0.6, 0.6),
        frame_points=[(0.1, 0.1), (0.7, 0.1), (0.7, 0.7), (0.1, 0.7)],
        z_index=0,
        image_asset=str(blue),
    )
    top = _panel(
        panel_id="t",
        bbox=(0.3, 0.3, 0.6, 0.6),
        frame_points=[(0.3, 0.3), (0.9, 0.3), (0.9, 0.9), (0.3, 0.9)],
        z_index=1,
        image_asset=str(red),
    )
    for panel in sorted([top, bottom], key=lambda item: item.z_index):
        draw_panel_art(page, draw, panel, _panel_box_px(panel), export_dir=None)
    page_w, page_h = PAGE_SIZE
    # 重なり中心は手前(z=1)の赤。
    cx, cy = int(0.5 * page_w), int(0.5 * page_h)
    overlap = page.getpixel((cx, cy))
    assert overlap[0] > 200 and overlap[2] < 80
    # 重なりの外（bottomのみ）は青。
    only_bottom = page.getpixel((int(0.15 * page_w), int(0.15 * page_h)))
    assert only_bottom[2] > 200 and only_bottom[0] < 80
    # ポリゴン外（どちらも覆わない右上隅近く）は白地のまま。
    outside = page.getpixel((int(0.95 * page_w), int(0.02 * page_h)))
    assert outside[0] > 200 and outside[1] > 200 and outside[2] > 200


def test_shape_points_migrates_to_frame_points_for_polygon_mask(tmp_path) -> None:
    page = Image.new("RGBA", PAGE_SIZE, (255, 255, 255, 255))
    draw = ImageDraw.Draw(page)
    red = tmp_path / "red.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(red)
    panel = _panel(
        bbox=(0.1, 0.1, 0.4, 0.4),
        shape_points=[(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)],
        image_asset=str(red),
    )
    assert panel.shape_points is None
    assert panel.frame_points is not None

    draw_panel_art(page, draw, panel, _panel_box_px(panel), export_dir=None)

    page_w, page_h = PAGE_SIZE
    inside = page.getpixel((int(0.3 * page_w), int(0.2 * page_h)))
    assert inside[0] > 200 and inside[1] < 80
    # bbox内だが三角形ポリゴン外の左下は白地のまま。
    outside_shape = page.getpixel((int(0.12 * page_w), int(0.48 * page_h)))
    assert outside_shape[0] > 200 and outside_shape[1] > 200 and outside_shape[2] > 200


def test_panel_text_is_layered_with_panel_z_index(tmp_path) -> None:
    export_dir = tmp_path / "exports"
    project_id = "proj"
    asset_dir = export_dir / project_id
    asset_dir.mkdir(parents=True)
    bottom_image = asset_dir / "bottom.png"
    top_image = asset_dir / "top.png"
    Image.new("RGB", (100, 100), (255, 230, 230)).save(bottom_image)
    Image.new("RGB", (100, 100), (0, 0, 255)).save(top_image)
    bottom = Panel(
        panel_id="bottom",
        bbox=(0.1, 0.1, 0.8, 0.8),
        shot="bottom",
        image_asset=f"{project_id}/bottom.png",
        z_index=0,
        dialogue=[
            Dialogue(
                speaker="a",
                text="下",
                box=(0.25, 0.25, 0.5, 0.5),
                font_size=60,
                tail=None,
            )
        ],
    )
    top = Panel(
        panel_id="top",
        bbox=(0.25, 0.25, 0.5, 0.5),
        shot="top",
        image_asset=f"{project_id}/top.png",
        z_index=2,
    )
    manga = MangaProject(
        title="t",
        pages=[Page(page=1, theme="t", layout_template="x", panels=[bottom, top])],
    )

    target, _warnings = render_project_page(project_id, manga, 1, export_dir)

    with Image.open(target).convert("RGB") as rendered:
        center = rendered.getpixel((PAGE_SIZE[0] // 2, PAGE_SIZE[1] // 2))
    assert center[2] > 180 and center[0] < 80
