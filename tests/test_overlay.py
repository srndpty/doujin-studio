"""overlay描画のテスト。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.app.renderer import render_project_page
from backend.app.schemas import (
    MangaProject,
    OverlayElement,
    Page,
    Panel,
)


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
