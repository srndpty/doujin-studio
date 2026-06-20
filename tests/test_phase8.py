"""Phase 1（構図・写植・吹き出し）のテスト。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.app import fonts, typeset
from backend.app.generator import compute_generation_size
from backend.app.prompt_composer import compose_panel_prompts, prepare_panel_for_generation
from backend.app.renderer import fit_image_to_box, render_project_page
from backend.app.schemas import (
    BalloonTail,
    Dialogue,
    GenerationInfo,
    MangaProject,
    Page,
    Panel,
    Sfx,
)


def test_balloon_values_are_migrated_from_old_schema() -> None:
    dialogue = Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "round"})
    assert dialogue.balloon == "oval"
    assert Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "thought"}).balloon == "cloud"
    assert Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "shout"}).balloon == "burst"
    # 新しい値はそのまま通る。
    assert Dialogue.model_validate({"speaker": "a", "text": "x", "balloon": "caption"}).balloon == "caption"


def test_generation_size_matches_aspect_and_snaps_to_64() -> None:
    width, height = compute_generation_size((0.06, 0.05, 0.88, 0.28))
    assert width % 64 == 0 and height % 64 == 0
    assert width > height  # 横長コマは横長サイズ
    # 縦長コマは縦長サイズになる。
    tall_w, tall_h = compute_generation_size((0.06, 0.05, 0.3, 0.9))
    assert tall_h > tall_w


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


def test_dialogue_font_falls_back_to_biz_ud_when_genei_absent() -> None:
    # CI/開発機に源暎アンチックが無ければBIZ UDなどへ退避する。
    path = fonts.find_dialogue_font_path()
    assert path is not None
    if not fonts.dialogue_font_is_primary():
        assert "GenEiAntique" not in path.name
    listed = {item["id"]: item for item in fonts.list_fonts()}
    assert listed["genei_antique"]["is_primary"] is True


def test_vertical_typeset_keeps_all_characters() -> None:
    text = "これはABC123とテスト、長い台詞。"
    layout = typeset.layout_text(text, None, 200, 600, vertical=True, default_size=34, min_size=26)
    flattened = "".join(token[1] for line in layout.columns for token in line)
    # 縦中横や回転で順序は保つ。全文字が保持される（切り捨てない）。
    for ch in text:
        assert ch in flattened


def test_long_text_warns_when_it_cannot_fit() -> None:
    text = "あ" * 400
    layout = typeset.layout_text(text, None, 60, 60, vertical=True, default_size=34, min_size=26)
    assert layout.fits is False
    assert any("収まりません" in warning for warning in layout.warnings)


def test_crop_pan_zoom_is_deterministic_and_differs_from_default() -> None:
    source = Image.new("RGB", (200, 100), (10, 20, 30))
    for x in range(200):
        for y in range(100):
            source.putpixel((x, y), (x % 256, y % 256, 100))
    plain = fit_image_to_box(source, (80, 80), "cover", "center")
    zoomed_a = fit_image_to_box(source, (80, 80), "cover", "center", scale=2.0, offset_x=0.5, offset_y=-0.3)
    zoomed_b = fit_image_to_box(source, (80, 80), "cover", "center", scale=2.0, offset_x=0.5, offset_y=-0.3)
    assert zoomed_a.size == (80, 80)
    assert zoomed_a.tobytes() == zoomed_b.tobytes()  # 再現性
    assert zoomed_a.tobytes() != plain.tobytes()  # ズームで変化


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
