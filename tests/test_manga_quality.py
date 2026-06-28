"""4ページ漫画の商用品質改善（領域1〜5）のテスト。"""

from __future__ import annotations

import pytest
from PIL import Image as PILImage

from backend.app import preflight, renderer, story
from backend.app.generator import suggest_candidate_count
from backend.app.prompt_composer import compose_panel_prompts
from backend.app.renderer import _panel_box_px, compute_bubble_layout
from backend.app.rendering import build_production_status
from backend.app.schemas import (
    BalloonTail,
    Character,
    Dialogue,
    MangaProject,
    Page,
    Panel,
    PanelCharacter,
    ScriptCharacter,
    ScriptDialogue,
    ScriptPage,
    ScriptPanel,
    ScriptStage,
    Sfx,
    TypographySettings,
)

# --- 領域2/3: 台詞種別 → 吹き出し自動選択 ---


def test_dialogue_kind_selects_balloon() -> None:
    assert Dialogue(speaker="a", text="やあ").balloon == "oval"
    assert Dialogue(speaker="a", text="…", kind="monologue").balloon == "caption"
    assert Dialogue(speaker="a", text="その時", kind="narration").balloon == "caption"
    assert Dialogue(speaker="a", text="うわっ", kind="shout").balloon == "burst"
    # 明示的に指定した非oval形状は尊重する（kindに上書きされない）。
    assert Dialogue(speaker="a", text="…", kind="monologue", balloon="caption").balloon == "caption"
    # ovalを手動固定（balloon_auto=False）した独白は、cloudへ書き換えずovalのまま保持する。
    assert (
        Dialogue(
            speaker="a", text="……", kind="monologue", balloon="oval", balloon_auto=False
        ).balloon
        == "oval"
    )


def test_saved_json_kind_change_updates_balloon() -> None:
    # 新規JSON（balloon_auto=True）の再編集でkindを変えると、既定形状へ追従する。
    payload = Dialogue(speaker="a", text="本文").model_dump()
    assert payload["balloon"] == "oval" and payload["balloon_auto"] is True
    payload["kind"] = "narration"
    assert Dialogue.model_validate(payload).balloon == "caption"
    # 手動固定（balloon_auto=False）は再編集でも保持される。
    manual = Dialogue(
        speaker="a", text="本文", kind="monologue", balloon="oval", balloon_auto=False
    ).model_dump()
    assert manual["balloon_auto"] is False
    manual["kind"] = "narration"
    assert Dialogue.model_validate(manual).balloon == "oval"


def test_legacy_json_balloon_auto_backfill() -> None:
    # balloon_auto導入前の旧JSON: oval既定は自動扱いへbackfillし、kind変更で追従する。
    legacy = {"speaker": "a", "text": "本文", "balloon": "oval", "kind": "narration"}
    assert Dialogue.model_validate(legacy).balloon == "caption"
    # 旧JSONで明示された非oval形状は手動扱いで保持する。
    legacy_shape = {"speaker": "a", "text": "本文", "balloon": "cloud", "kind": "narration"}
    assert Dialogue.model_validate(legacy_shape).balloon == "cloud"


def test_tail_follows_on_screen_at_draw_time() -> None:
    # on_screenは描画時に評価する。モデルでtailを自動生成しない（再編集で復活できる）。
    off = Dialogue(speaker="a", text="画面外の声", on_screen=False)
    assert off.tail is None
    assert renderer.dialogue_draws_tail(off) is False
    on = Dialogue(speaker="a", text="画面内の声", on_screen=True)
    assert renderer.dialogue_draws_tail(on) is True
    # 画面外→保存→画面内へ戻すと、既定のしっぽが再び描かれる（復活する）。
    restored = Dialogue.model_validate({**off.model_dump(), "on_screen": True})
    assert restored.tail is None
    assert renderer.dialogue_draws_tail(restored) is True
    # 明示的に無効化したtailは画面内でも描かない。
    disabled = Dialogue(speaker="a", text="x", tail=BalloonTail(enabled=False))
    assert renderer.dialogue_draws_tail(disabled) is False


# --- 領域3: 収まり判定と描画領域の一致（はみ出し0） ---


def _single_panel(dialogue: Dialogue) -> Panel:
    return Panel(panel_id="p01_01", bbox=(0.05, 0.05, 0.9, 0.9), shot="t", dialogue=[dialogue])


def test_bubble_text_area_inside_bubble_and_oval() -> None:
    panel = _single_panel(Dialogue(speaker="a", text="これはテスト台詞です"))
    box = _panel_box_px(panel)
    result = compute_bubble_layout(panel.dialogue[0], box, TypographySettings())
    bx0, by0, bx1, by1 = result.bubble
    tx0, ty0, tx1, ty1 = result.text_area
    # テキスト矩形は吹き出し外形の内側に収まる。
    assert bx0 <= tx0 and by0 <= ty0 and tx1 <= bx1 and ty1 <= by1
    # 楕円の内接条件: テキスト矩形の四隅が楕円内（(x/a)^2+(y/b)^2<=1）。
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    a, b = (bx1 - bx0) / 2, (by1 - by0) / 2
    for corner in ((tx0, ty0), (tx1, ty1), (tx0, ty1), (tx1, ty0)):
        norm = ((corner[0] - cx) / a) ** 2 + ((corner[1] - cy) / b) ** 2
        assert norm <= 1.0 + 1e-6


def test_preflight_dialogue_clipped_is_error() -> None:
    panel = _single_panel(Dialogue(speaker="a", text="あ" * 400, box=(0.0, 0.0, 0.1, 0.05)))
    panel.bbox = (0.02, 0.02, 0.16, 0.08)
    manga = MangaProject(
        title="t", pages=[Page(page=1, theme="t", layout_template="o", panels=[panel])]
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    assert any(i.code == "dialogue_clipped" and i.level == "error" for i in issues)


def test_explicitly_small_dialogue_is_not_flagged_as_shrunk() -> None:
    # font_sizeを明示的に小さくした台詞は、縮小なしで描けるなら警告にならない（Low回帰）。
    panel = _single_panel(Dialogue(speaker="a", text="やあ", font_size=20))
    manga = MangaProject(
        title="t",
        typography=TypographySettings(default_font_size=32),
        pages=[Page(page=1, theme="t", layout_template="o", panels=[panel])],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    assert not any(i.code == "dialogue_overflow" for i in issues)


# --- 領域4: 擬音styleプリセットと決定的ゆらぎ ---


def test_sfx_render_is_deterministic_and_style_varies() -> None:
    base = Sfx(text="ドカーン", style="handwritten", font_size=60)
    fill, stroke = (20, 20, 20), (255, 255, 255)
    first = renderer._render_sfx_tile(base, fill, stroke)
    second = renderer._render_sfx_tile(base.model_copy(deep=True), fill, stroke)
    # 同じ入力なら必ず同じ描画（同seed=同結果）。
    assert first.tobytes() == second.tobytes()
    quiet = renderer._render_sfx_tile(
        Sfx(text="ドカーン", style="quiet", font_size=60), fill, stroke
    )
    # styleごとに視覚差がある（ゆらぎ量が違うのでバイト列が一致しない）。
    assert first.tobytes() != quiet.tobytes()


def test_sfx_newline_is_preserved_as_break() -> None:
    fill, stroke = (20, 20, 20), (255, 255, 255)
    one_line = renderer._render_sfx_tile(Sfx(text="ドンバン", font_size=60), fill, stroke)
    two_lines = renderer._render_sfx_tile(Sfx(text="ドン\nバン", font_size=60), fill, stroke)
    # 横書きで改行が行区切りとして保持され、2行のタイルは1行より縦に高く・横に狭くなる。
    assert two_lines.height > one_line.height
    assert two_lines.width < one_line.width


def test_sfx_jitter_differs_by_position() -> None:
    # 同じ文字列でも位置が違えばゆらぎが変わる（複製した印象にならない）。
    fill, stroke = (20, 20, 20), (255, 255, 255)
    upper = renderer._render_sfx_tile(
        Sfx(text="ドン", position="upper_left", font_size=60), fill, stroke
    )
    lower = renderer._render_sfx_tile(
        Sfx(text="ドン", position="lower_right", font_size=60), fill, stroke
    )
    assert upper.size == lower.size  # タイル寸法は同じ
    assert upper.tobytes() != lower.tobytes()  # ゆらぎは異なる


def test_render_refreshes_sfx_font_cache() -> None:
    # 描画の入口で擬音フォントの探索キャッシュが破棄される（実行中の追加が反映される）。
    from backend.app.fonts import find_sfx_font_path

    find_sfx_font_path("dummy-preferred")
    assert find_sfx_font_path.cache_info().currsize >= 1
    renderer._refresh_sfx_font_cache()
    assert find_sfx_font_path.cache_info().currsize == 0


def _signature_manga(position: str) -> tuple[MangaProject, Page]:
    panel = Panel(
        panel_id="p01_01",
        bbox=(0.05, 0.05, 0.9, 0.9),
        shot="t",
        characters=["mika"],
        character_layout=[PanelCharacter(id="mika", position=position)],
    )
    manga = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
        pages=[Page(page=1, theme="t", layout_template="o", panels=[panel])],
    )
    return manga, manga.pages[0]


def test_render_signature_includes_character_layout_position() -> None:
    # しっぽ方向を変えるcharacter_layout.positionは描画hashに反映される（P2）。
    from backend.app.rendering import page_render_hash

    m1, p1 = _signature_manga("upper_left")
    m2, p2 = _signature_manga("lower_right")
    assert page_render_hash(m1, p1) != page_render_hash(m2, p2)


def test_render_signature_includes_sfx_font(tmp_path, monkeypatch) -> None:
    # 擬音フォントの差し替えで描画hashが変わる（不変アセットの内容不一致を防ぐ・P1）。
    from backend.app import fonts, rendering

    font_a = tmp_path / "a.ttf"
    font_a.write_bytes(b"a")
    font_b = tmp_path / "b.ttf"
    font_b.write_bytes(b"b")
    manga, page = _signature_manga("center")
    monkeypatch.setattr(fonts, "find_sfx_font_path", lambda *args, **kwargs: font_a)
    hash_a = rendering.page_render_hash(manga, page)
    monkeypatch.setattr(fonts, "find_sfx_font_path", lambda *args, **kwargs: font_b)
    hash_b = rendering.page_render_hash(manga, page)
    assert hash_a != hash_b


def test_sfx_style_presets_differ() -> None:
    assert (
        renderer.sfx_style_params("handwritten").jitter_rot
        > renderer.sfx_style_params("quiet").jitter_rot
    )
    assert (
        renderer.sfx_style_params("impact").jitter_scale
        > renderer.sfx_style_params("quiet").jitter_scale
    )


# --- 領域4/5: プリフライトのSFX衝突・人物設定不足 ---


def test_preflight_detects_sfx_overlap() -> None:
    # 同じ中心座標へ2つの擬音を置くと重なりを検出する。
    panel = Panel(
        panel_id="p01_01",
        bbox=(0.05, 0.05, 0.9, 0.9),
        shot="t",
        sfx=[
            Sfx(text="バチッ", box=(0.5, 0.5), font_size=60),
            Sfx(text="ポコッ", box=(0.5, 0.5), font_size=60),
        ],
    )
    manga = MangaProject(
        title="t", pages=[Page(page=1, theme="t", layout_template="o", panels=[panel])]
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    assert any(i.code == "sfx_overlap" for i in issues)


def test_preflight_detects_cross_panel_sfx_overlap() -> None:
    # 隣接コマの擬音がガター上（コマ外）で重なるケースもページ全体で検出する。
    left = Panel(
        panel_id="p01_01",
        bbox=(0.05, 0.4, 0.4, 0.2),
        shot="t",
        sfx=[Sfx(text="ドン", box=(1.0, 0.5), font_size=80)],
    )
    right = Panel(
        panel_id="p01_02",
        bbox=(0.45, 0.4, 0.4, 0.2),
        shot="t",
        sfx=[Sfx(text="ドン", box=(0.0, 0.5), font_size=80)],
    )
    manga = MangaProject(
        title="t", pages=[Page(page=1, theme="t", layout_template="o", panels=[left, right])]
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    overlap = [i for i in issues if i.code == "sfx_overlap"]
    assert overlap and "p01_01 と p01_02" in overlap[0].message


def test_preflight_flags_character_setup_incomplete() -> None:
    # trigger=表示名のみ・LoRA/参照画像なし → 同一性を保てない警告。
    weak = Character(id="rika", display_name="莉嘉", trigger_prompt="莉嘉")
    panel = Panel(panel_id="p01_01", bbox=(0.05, 0.05, 0.9, 0.9), shot="t", characters=["rika"])
    manga = MangaProject(
        title="t",
        characters=[weak],
        pages=[Page(page=1, theme="t", layout_template="o", panels=[panel])],
    )
    issues = preflight.preflight_page(manga, manga.pages[0])
    incomplete = [i for i in issues if i.code == "character_setup_incomplete"]
    assert len(incomplete) == 1  # ページ内で1回に集約される


# --- 領域1: subject_mode自動分類と画面外話者の非描画 ---


def test_classify_subject_mode() -> None:
    assert (
        story.classify_subject_mode(
            ScriptPanel(shot="手元のアップ", visual_prompt="close-up of hands")
        )
        == "hand_insert"
    )
    assert (
        story.classify_subject_mode(
            ScriptPanel(shot="背景", visual_prompt="establishing shot of a town")
        )
        == "background"
    )
    assert (
        story.classify_subject_mode(
            ScriptPanel(shot="小箱のアップ", visual_prompt="a small box, product")
        )
        == "prop_insert"
    )
    # 台詞があるコマは人物コマとして扱う。
    assert (
        story.classify_subject_mode(
            ScriptPanel(shot="手元", dialogue=[ScriptDialogue(speaker="a", text="やあ")])
        )
        == "character_scene"
    )
    # 人物を明示した無言の引き絵は、background語を含んでも人物コマとして尊重する。
    assert (
        story.classify_subject_mode(
            ScriptPanel(
                shot="引き", visual_prompt="wide shot of a room, background", characters=["美嘉"]
            )
        )
        == "character_scene"
    )


def test_named_silent_wide_shot_keeps_characters() -> None:
    # 人物名あり＋background語の無言コマで、台本指定の人物が消えないこと（Medium回帰）。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="引き",
        visual_prompt="wide shot of a town, background, scenery",
        characters=["美嘉"],
        dialogue=[],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base, page_characters={1: ["美嘉"]})
    drawn = manga.pages[0].panels[0]
    assert drawn.subject_mode == "character_scene"
    assert drawn.characters == ["mika"]


def test_offscreen_narration_panel_does_not_get_page_characters() -> None:
    # 同一ページに人物コマと「背景＋画面外ナレーション」コマを置き、後者へページ人物が
    # 再混入しないこと（人物LoRA混入の防止）を検証する。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    character_panel = ScriptPanel(
        shot="bust",
        visual_prompt="a girl talking",
        characters=["美嘉"],
        dialogue=[ScriptDialogue(speaker="美嘉", text="やあ", kind="speech")],
    )
    narration_panel = ScriptPanel(
        shot="背景",
        visual_prompt="empty room, scenery",
        characters=[],
        dialogue=[
            ScriptDialogue(
                speaker="美嘉",
                text="あの日のことを思い出す",
                kind="narration",
                on_screen=False,
            )
        ],
    )
    script = ScriptStage(
        pages=[
            ScriptPage(page=page, panels=[character_panel, narration_panel]) for page in range(1, 5)
        ]
    )
    # ページ構成の登場人物（フォールバック候補）を渡しても再混入しないこと。
    manga = story.script_to_manga(script, base, page_characters={1: ["美嘉"]})
    person, narration = manga.pages[0].panels
    assert person.characters == ["mika"]
    assert narration.subject_mode == "background"
    assert narration.characters == []
    assert narration.dialogue[0].kind == "narration"
    assert narration.dialogue[0].on_screen is False


# --- 領域2: 生成後の編集チェック ---


def test_review_script_keeps_named_speaker_dialogue() -> None:
    # 話者付きの自然な台詞（純カタカナ）は擬音へ移動せず内容を保持する。
    data = {
        "pages": [
            {
                "page": 1,
                "panels": [
                    {
                        "characters": ["美嘉"],
                        "dialogue": [{"speaker": "美嘉", "text": "ハイ！", "kind": "speech"}],
                        "sfx": [],
                    }
                ],
            }
        ]
    }
    fixed, _warnings = story.review_script(data)
    panel = fixed["pages"][0]["panels"][0]
    assert panel["dialogue"][0]["text"] == "ハイ！"
    assert panel["sfx"] == []


def test_review_script_warns_but_does_not_destroy_content() -> None:
    data = {
        "pages": [
            {
                "page": 1,
                "panels": [
                    {
                        "characters": ["美嘉"],
                        "dialogue": [
                            {"speaker": "", "text": "ドカーン", "kind": "speech"},
                            {"speaker": "美嘉", "text": "（やってしまった）", "kind": "speech"},
                            {"speaker": "美嘉", "text": "やめろ！！", "kind": "speech"},
                        ],
                        "sfx": [{"text": "トドメ"}, {"text": "ザッ"}],
                    }
                ],
            }
        ]
    }
    fixed, warnings = story.review_script(data)
    panel = fixed["pages"][0]["panels"][0]
    texts = [line["text"] for line in panel["dialogue"]]
    # 擬音らしき台詞も移動・削除せず保持し、警告だけ出す（自動での内容破壊をしない）。
    assert "ドカーン" in texts
    assert any("ドカーン" in warning for warning in warnings)
    # kindの是正（独白・叫び）は非破壊なので自動で行う。
    monologue = next(line for line in panel["dialogue"] if line["text"] == "（やってしまった）")
    assert monologue["kind"] == "monologue"
    shout = next(line for line in panel["dialogue"] if line["text"] == "やめろ！！")
    assert shout["kind"] == "shout"
    # 場面非対応らしき擬音も削除せず保持し、警告だけ出す。
    assert any(s["text"] == "トドメ" for s in panel["sfx"])
    assert any("トドメ" in warning for warning in warnings)


def test_review_script_warns_on_anonymous_onomatopoeia_dialogue() -> None:
    # 話者未確定の「ダメ！」のような自然な会話を擬音へ移さず保持する（Medium回帰）。
    data = {
        "pages": [
            {
                "page": 1,
                "panels": [
                    {
                        "characters": [],
                        "dialogue": [{"speaker": "", "text": "ダメ！", "kind": "speech"}],
                        "sfx": [],
                    }
                ],
            }
        ]
    }
    fixed, warnings = story.review_script(data)
    panel = fixed["pages"][0]["panels"][0]
    assert panel["dialogue"][0]["text"] == "ダメ！"
    assert panel["sfx"] == []
    assert any("ダメ！" in warning for warning in warnings)


# --- 領域5: 候補数の自動提案 ---


def test_suggest_candidate_count() -> None:
    plain = Panel(panel_id="a", bbox=(0.05, 0.05, 0.4, 0.4), shot="t", emphasis=2)
    showcase = Panel(
        panel_id="b",
        bbox=(0.05, 0.05, 0.9, 0.9),
        shot="t",
        emphasis=5,
        role="punchline",
        characters=["x", "y"],
    )
    assert suggest_candidate_count(plain) == 2
    assert suggest_candidate_count(showcase) == 4
    assert suggest_candidate_count(plain) <= suggest_candidate_count(showcase)
    # base=candidate_countを下限にすると、通常コマは増えず見せ場だけ増える（UI説明と一致）。
    assert suggest_candidate_count(plain, base=1) == 1
    assert suggest_candidate_count(showcase, base=1) == 4


# --- 領域1: character_layoutの整合検証とプロンプト反映 ---


def test_character_layout_must_subset_characters() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Panel(
            panel_id="p01_01",
            bbox=(0.05, 0.05, 0.9, 0.9),
            shot="t",
            characters=["mika"],
            character_layout=[PanelCharacter(id="rika")],  # charactersに無いID
        )


def test_script_directives_flow_to_prompt_per_character() -> None:
    # 2人物の台本ディレクション（位置・表情・動作）が、変換後のpromptに人物別で残ること。
    base = MangaProject(
        title="t",
        characters=[
            Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl"),
            Character(id="rika", display_name="莉嘉", trigger_prompt="rika 1girl"),
        ],
    )
    panel = ScriptPanel(
        shot="two shot",
        visual_prompt="two girls",
        characters=["美嘉", "莉嘉"],
        character_directives=[
            ScriptCharacter(
                name="美嘉", position="upper_left", expression="smiling", action="waving hand"
            ),
            ScriptCharacter(name="莉嘉", position="lower_right", expression="crying"),
        ],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base)
    drawn = manga.pages[0].panels[0]
    layout = {entry.id: entry for entry in drawn.character_layout}
    assert layout["mika"].expression == "smiling" and layout["mika"].action == "waving hand"
    assert layout["mika"].position == "upper_left"
    assert layout["mika"].region_box == (0.02, 0.02, 0.46, 0.46)
    assert layout["rika"].expression == "crying" and layout["rika"].position == "lower_right"
    assert layout["rika"].region_box == (0.52, 0.52, 0.46, 0.46)
    positive, _negative = compose_panel_prompts(manga, drawn)
    for token in ("smiling", "waving hand", "crying", "on the upper left", "on the lower right"):
        assert token in positive


def test_script_directive_region_box_is_preserved() -> None:
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="solo",
        visual_prompt="a girl",
        character_directives=[
            ScriptCharacter(name="美嘉", position="center", region_box=(0.1, 0.2, 0.3, 0.4))
        ],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base)
    assert manga.pages[0].panels[0].character_layout[0].region_box == (0.1, 0.2, 0.3, 0.4)


def test_script_boxes_accept_explicit_xyxy_from_llm() -> None:
    panel = ScriptPanel(
        shot="close",
        text_safe_area_format="xyxy",
        text_safe_area=[0.65, 0.72, 0.92, 0.88],
        character_directives=[
            {
                "name": "美嘉",
                "position": "center",
                "region_box_format": "xyxy",
                "region_box": [0.65, 0.72, 0.92, 0.88],
            }
        ],
    )
    assert panel.text_safe_area == pytest.approx((0.65, 0.72, 0.27, 0.16))
    assert panel.character_directives[0].region_box == pytest.approx((0.65, 0.72, 0.27, 0.16))


def test_ambiguous_box_without_format_stays_xywh() -> None:
    panel = ScriptPanel(
        shot="close",
        text_safe_area=[0.1, 0.1, 0.7, 0.7],
        character_directives=[
            {
                "name": "美嘉",
                "position": "center",
                "region_box": [0.1, 0.1, 0.7, 0.7],
            }
        ],
    )
    assert panel.text_safe_area == pytest.approx((0.1, 0.1, 0.7, 0.7))
    assert panel.character_directives[0].region_box == pytest.approx((0.1, 0.1, 0.7, 0.7))


def test_xyxy_without_format_is_validation_error() -> None:
    with pytest.raises(ValueError, match="text_safe_area"):
        ScriptPanel(shot="close", text_safe_area=[0.65, 0.72, 0.92, 0.88])


def test_directive_only_character_is_kept() -> None:
    # charactersへの列挙を漏らしても、character_directivesの人物は取りこぼさない（Medium回帰）。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="solo",
        visual_prompt="a girl",
        characters=[],  # 明示列挙は漏れている
        character_directives=[
            ScriptCharacter(name="美嘉", position="center", expression="smiling")
        ],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base)
    drawn = manga.pages[0].panels[0]
    assert drawn.subject_mode == "character_scene"
    assert drawn.characters == ["mika"]
    assert drawn.character_layout[0].expression == "smiling"


def test_default_region_splits_same_position_characters() -> None:
    base = MangaProject(
        title="t",
        characters=[
            Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl"),
            Character(id="rika", display_name="莉嘉", trigger_prompt="rika 1girl"),
        ],
    )
    panel = ScriptPanel(
        shot="duo",
        characters=["美嘉", "莉嘉"],
        character_directives=[
            ScriptCharacter(name="美嘉", position="center"),
            ScriptCharacter(name="莉嘉", position="center"),
        ],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base)
    boxes = [entry.region_box for entry in manga.pages[0].panels[0].character_layout]
    assert boxes[0] != boxes[1]
    assert boxes[0][0] < boxes[1][0]


def test_default_region_handles_many_same_position_characters() -> None:
    for total in (8, 9):
        boxes = [story.default_region_box("center", index, total) for index in range(total)]
        assert len(set(boxes)) == total
        for x, y, width, height in boxes:
            assert x >= 0
            assert y >= 0
            assert width > 0
            assert height > 0
            assert x + width <= 1
            assert y + height <= 1


def test_preflight_warns_overlapping_character_regions() -> None:
    panel = _quality_panel(
        characters=["mika", "rika"],
        character_layout=[
            PanelCharacter(id="mika", region_box=(0.1, 0.1, 0.6, 0.8)),
            PanelCharacter(id="rika", region_box=(0.12, 0.12, 0.6, 0.8)),
        ],
    )
    issues = _quality_issues(panel)
    assert any(i.code == "character_region_overlap" for i in issues)


def test_unresolved_directive_does_not_inject_page_characters() -> None:
    # 解決できないdirective名があっても、同ページの別人物がfallbackで混入しない（Medium回帰）。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="solo",
        visual_prompt="someone",
        characters=[],
        character_directives=[ScriptCharacter(name="未知名", position="center")],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base, page_characters={1: ["美嘉"]})
    drawn = manga.pages[0].panels[0]
    # 明示意図（未知のdirective）があるので、ページ人物「美嘉」を勝手に入れない。
    assert drawn.characters == []


def test_empty_speaker_dialogue_keeps_background_mode() -> None:
    # speaker省略の台詞本文だけの背景コマは人物コマと断定せず、ページ人物も混入しない（Medium回帰）。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="背景",
        visual_prompt="empty background, scenery",
        characters=[],
        dialogue=[ScriptDialogue(speaker="", text="その時…")],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    manga = story.script_to_manga(script, base, page_characters={1: ["美嘉"]})
    drawn = manga.pages[0].panels[0]
    assert drawn.subject_mode == "background"
    assert drawn.characters == []


def test_script_to_manga_warns_on_unresolved_character() -> None:
    # 解決不能な人物名（誤字・表記揺れ）は警告として収集される（Low回帰）。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="solo",
        visual_prompt="someone",
        characters=["美香"],  # 誤字（登録は「美嘉」）
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    warnings: list[str] = []
    manga = story.script_to_manga(script, base, warnings=warnings)
    assert manga.pages[0].panels[0].characters == []
    assert any("美香" in warning for warning in warnings)


def test_unresolved_on_screen_speaker_is_warned() -> None:
    # 画面内のspeech話者が誤字で解決できない場合も警告対象に含める（Medium回帰）。
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = ScriptPanel(
        shot="bust",
        visual_prompt="a girl",
        characters=[],
        dialogue=[ScriptDialogue(speaker="美香", text="やあ", kind="speech", on_screen=True)],
    )
    script = ScriptStage(pages=[ScriptPage(page=page, panels=[panel]) for page in range(1, 5)])
    warnings: list[str] = []
    story.script_to_manga(script, base, warnings=warnings)
    assert any("美香" in warning for warning in warnings)


def test_merge_unresolved_warnings_replaces_stale_category() -> None:
    # 古い未解決警告は再計算で置換され、解決後は残らない。他カテゴリは保持する（Low回帰）。
    prefix = story.UNRESOLVED_CHARACTER_PREFIX
    existing = [
        f"{prefix}p01_01: 「美香」を登録キャラクターに解決できません",
        "1ページ コマ1: 別カテゴリの警告",
    ]
    # 解決済み（unresolved空）なら誤字警告は消え、他カテゴリは残る。
    assert story.merge_unresolved_warnings(existing, []) == ["1ページ コマ1: 別カテゴリの警告"]
    # 別の誤字が残る場合はその新警告へ置換される。
    fresh = [f"{prefix}p02_01: 「莉香」を登録キャラクターに解決できません"]
    assert story.merge_unresolved_warnings(existing, fresh) == [
        "1ページ コマ1: 別カテゴリの警告",
        *fresh,
    ]


def test_review_script_no_false_warning_for_directive_only_page() -> None:
    # character_directivesだけで人物を指定した無台詞ページに誤警告を出さない（Low回帰）。
    data = {
        "pages": [
            {
                "page": 1,
                "panels": [
                    {
                        "characters": [],
                        "character_directives": [{"name": "美嘉", "expression": "smiling"}],
                        "dialogue": [],
                        "sfx": [],
                    }
                ],
            }
        ]
    }
    _fixed, warnings = story.review_script(data)
    assert not any("登場人物が指定されていません" in warning for warning in warnings)


def test_script_character_position_normalizes_variants() -> None:
    # 表記ゆれ（ハイフン・大文字・別名）を既定position語へ正規化する（Low回帰）。
    assert ScriptCharacter(name="a", position="upper-left").position == "upper_left"
    assert ScriptCharacter(name="a", position="Upper_Left").position == "upper_left"
    assert ScriptCharacter(name="a", position="top-right").position == "upper_right"
    assert ScriptCharacter(name="a", position="middle").position == "center"
    assert ScriptCharacter(name="a", position="nonsense").position == "center"


def test_character_layout_feeds_prompt() -> None:
    manga = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    panel = Panel(
        panel_id="p01_01",
        bbox=(0.05, 0.05, 0.9, 0.9),
        shot="t",
        characters=["mika"],
        character_layout=[PanelCharacter(id="mika", expression="smiling", action="waving hand")],
    )
    positive, _negative = compose_panel_prompts(manga, panel)
    # character_layoutの表情・動作がプロンプトへ反映される（死んだ状態ではない）。
    assert "smiling" in positive
    assert "waving hand" in positive


# --- 漫画品質ゲート: 英字SFX・変形コマ・独白泡・しっぽ追従・画像メトリクス ---


def _quality_panel(**kwargs) -> Panel:
    base = {"panel_id": "p01_01", "bbox": (0.05, 0.05, 0.9, 0.9), "shot": "t"}
    base.update(kwargs)
    return Panel(**base)


def _quality_manga(panel: Panel, **manga_kwargs) -> MangaProject:
    return MangaProject(
        title="t",
        pages=[Page(page=1, theme="t", layout_template="o", panels=[panel])],
        **manga_kwargs,
    )


def _quality_issues(panel: Panel, **manga_kwargs):
    # export_dir=None: image_assetは絶対パスとしてそのまま解決される。
    manga = _quality_manga(panel, **manga_kwargs)
    return preflight.preflight_page(manga, manga.pages[0])


def test_english_sfx_normalized_to_japanese() -> None:
    assert story.normalize_sfx_text("bang") == "バン"
    assert story.normalize_sfx_text("Whoosh!") == "ヒュッ"
    assert story.normalize_sfx_text("step step") == "トッ トッ"
    # 未知の英字はそのまま返す（preflightで検出する）。
    assert story.normalize_sfx_text("kaboom") == "kaboom"
    # 台本→Sfx変換でも英字辞書が適用される。
    sfx = story.build_panel_sfx(ScriptPanel(sfx=["tap"]))
    assert sfx[0].text == "トン"


def test_preflight_flags_english_sfx() -> None:
    panel = _quality_panel(sfx=[Sfx(text="BOOM")])
    issues = _quality_issues(panel)
    assert any(i.code == "sfx_english_text" and i.level == "error" for i in issues)
    # 日本語擬音はエラーにならない。
    ok = _quality_panel(sfx=[Sfx(text="ドカーン")])
    assert not any(i.code == "sfx_english_text" for i in _quality_issues(ok))


def test_preflight_warns_cloud_monologue() -> None:
    dialogue = Dialogue(
        speaker="mika", text="…", kind="monologue", balloon="cloud", balloon_auto=False
    )
    panel = _quality_panel(dialogue=[dialogue])
    issues = _quality_issues(panel)
    assert any(i.code == "monologue_cloud_balloon" and i.category == "balloon" for i in issues)


def test_panel_shape_allowed_rule() -> None:
    assert story.panel_shape_allowed("action", "勢いのある動き") is True
    assert story.panel_shape_allowed("reveal", "dynamic impact") is True
    # role違い・意図語なしは不許可。
    assert story.panel_shape_allowed("dialogue", "勢いのある動き") is False
    assert story.panel_shape_allowed("action", "静かな会話") is False


def test_script_to_manga_shapes_only_motivated_panels() -> None:
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    action_panel = ScriptPanel(
        shot="t", role="action", composition_notes="爆発する勢い", characters=["美嘉"]
    )
    talk_panel = ScriptPanel(
        shot="t", role="dialogue", composition_notes="静かな会話", characters=["美嘉"]
    )
    script = ScriptStage(
        pages=[ScriptPage(page=p, panels=[action_panel, talk_panel]) for p in range(1, 5)]
    )
    manga = story.script_to_manga(script, base)
    panels = manga.pages[0].panels
    # 見せ場roleかつ動き・衝撃の意図があるコマだけが変形する。
    assert panels[0].shape_points is not None
    assert panels[1].shape_points is None


def test_preflight_flags_unmotivated_shape() -> None:
    slant = [(0.12, 0.0), (1.0, 0.0), (0.88, 1.0), (0.0, 1.0)]
    panel = _quality_panel(role="dialogue", shape_points=slant)
    issues = _quality_issues(panel)
    assert any(i.code == "unmotivated_panel_shape" and i.category == "layout" for i in issues)
    # 動機のある変形は警告しない。
    motivated = _quality_panel(role="action", composition_notes="爆発の勢い", shape_points=slant)
    assert not any(i.code == "unmotivated_panel_shape" for i in _quality_issues(motivated))


def test_preflight_flags_tail_not_pointing_to_speaker() -> None:
    far = Dialogue(speaker="mika", text="やあ", on_screen=True, tail=BalloonTail(tip=(0.95, 0.05)))
    panel = _quality_panel(
        characters=["mika"],
        character_layout=[PanelCharacter(id="mika", region_box=(0.0, 0.6, 0.2, 0.4))],
        dialogue=[far],
    )
    issues = _quality_issues(panel)
    assert any(i.code == "tail_not_pointing_to_speaker" and i.category == "balloon" for i in issues)


def test_preflight_tail_pointing_to_speaker_is_ok() -> None:
    near = Dialogue(speaker="mika", text="やあ", on_screen=True, tail=BalloonTail(tip=(0.1, 0.8)))
    panel = _quality_panel(
        characters=["mika"],
        character_layout=[PanelCharacter(id="mika", region_box=(0.0, 0.6, 0.2, 0.4))],
        dialogue=[near],
    )
    issues = _quality_issues(panel)
    assert not any(i.code == "tail_not_pointing_to_speaker" for i in issues)


def _image_panel(path, **kwargs) -> Panel:
    return _quality_panel(image_asset=str(path), **kwargs)


def test_image_metrics_empty_panel(tmp_path) -> None:
    path = tmp_path / "white.png"
    PILImage.new("RGB", (128, 128), (255, 255, 255)).save(path)
    issues = _quality_issues(_image_panel(path))
    assert any(i.code == "empty_panel_image" and i.level == "error" for i in issues)
    # 演出意図のある白背景・余韻コマは閾値を緩める。
    relaxed = _image_panel(path, role="aftermath", background_density="white")
    assert not any(i.code == "empty_panel_image" for i in _quality_issues(relaxed))


def test_image_metrics_subject_too_small(tmp_path) -> None:
    image = PILImage.new("RGB", (128, 128), (255, 255, 255))
    image.paste(PILImage.new("RGB", (30, 30), (210, 40, 40)), (50, 50))
    path = tmp_path / "small.png"
    image.save(path)
    issues = _quality_issues(_image_panel(path))
    assert any(i.code == "subject_too_small" for i in issues)
    assert not any(i.code == "empty_panel_image" for i in issues)


def test_image_metrics_monochrome_panel(tmp_path) -> None:
    path = tmp_path / "gray.png"
    PILImage.new("RGB", (128, 128), (128, 128, 128)).save(path)
    issues = _quality_issues(_image_panel(path))
    assert any(i.code == "monochrome_panel" and i.category == "style" for i in issues)
    # color_policy=mixedでは白黒禁止を強制しない。
    mixed = _quality_issues(_image_panel(path), color_policy="mixed")
    assert not any(i.code == "monochrome_panel" for i in mixed)


def test_image_metrics_good_image_passes(tmp_path) -> None:
    path = tmp_path / "good.png"
    PILImage.new("RGB", (128, 128), (220, 60, 40)).save(path)
    issues = _quality_issues(_image_panel(path))
    codes = {i.code for i in issues}
    assert not (codes & {"empty_panel_image", "subject_too_small", "monochrome_panel"})


# --- 品質ゲートの制作状態統合: preflightの要修正コマを production-status へ反映 ---


def test_production_status_surfaces_quality_errors(tmp_path) -> None:
    # 白紙コマ（empty_panel_image=error）が制作状態の quality_errors に出る。
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    PILImage.new("RGB", (128, 128), (255, 255, 255)).save(export_dir / "white.png")
    panel = _quality_panel(image_asset="white.png")
    manga = _quality_manga(panel)
    status = build_production_status("proj", manga, export_dir)
    page_status = status.pages[0]
    assert any(
        issue.code == "empty_panel_image" and issue.level == "error"
        for issue in page_status.quality_errors
    )
    # プロジェクト集約にも反映され、対象コマへ辿れるよう panel_id を保持する。
    aggregated = [issue for issue in status.quality_errors if issue.code == "empty_panel_image"]
    assert aggregated and aggregated[0].panel_id == "p01_01" and aggregated[0].page == 1
    # 既存blockers（採用未選択・未レンダリング）の意味は変えない。
    assert any("採用画像が未選択" in blocker for blocker in page_status.blockers)


def test_production_status_quality_error_prevents_complete(tmp_path) -> None:
    # 採用済み・レンダリング済みでも、品質エラーが残るページは制作完了にしない。
    export_dir = tmp_path / "exports"
    project_id = "proj"
    panel_asset = export_dir / project_id / "panels" / "candidate.png"
    panel_asset.parent.mkdir(parents=True)
    PILImage.new("RGB", (128, 128), (255, 255, 255)).save(panel_asset)

    panel = _quality_panel(
        image_asset=f"{project_id}/panels/candidate.png",
        selected_candidate_id="c1",
        image_candidates=[
            {
                "id": "c1",
                "asset": f"{project_id}/panels/candidate.png",
                "backend": "stub",
                "status": "done",
                "seed": 1,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    manga = _quality_manga(panel)
    page = manga.pages[0]
    from backend.app.rendering import page_render_hash

    page.render_status = "done"
    page.render_hash = page_render_hash(manga, page)
    page_asset = export_dir / project_id / "pages" / f"page_001.{page.render_hash}.png"
    page_asset.parent.mkdir(parents=True)
    PILImage.new("RGB", (1200, 1700), (255, 255, 255)).save(page_asset)
    page.render_asset = f"{project_id}/pages/{page_asset.name}"

    status = build_production_status(project_id, manga, export_dir)
    assert status.adopted_panels == status.total_panels
    assert status.rendered_pages == status.total_pages
    assert any(issue.code == "empty_panel_image" for issue in status.quality_errors)
    assert status.pages[0].status == "ready"
    assert status.status == "ready"
    assert status.blockers == []


def test_production_status_surfaces_region_warning(tmp_path) -> None:
    # 複数人物コマで region_box 不足（character_region_missing=warning）が出る。
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    panel = _quality_panel(
        characters=["mika", "rika"],
        character_layout=[
            PanelCharacter(id="mika", region_box=(0.0, 0.0, 0.4, 1.0)),
            PanelCharacter(id="rika"),
        ],
    )
    manga = _quality_manga(
        panel,
        characters=[
            Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl"),
            Character(id="rika", display_name="莉嘉", trigger_prompt="rika 1girl"),
        ],
    )
    status = build_production_status("proj", manga, export_dir)
    warnings = status.pages[0].quality_warnings
    assert any(issue.code == "character_region_missing" for issue in warnings)
    assert any(issue.code == "character_region_missing" for issue in status.quality_warnings)


def test_production_status_clean_project_has_no_quality_errors(tmp_path) -> None:
    # 良好な画像コマは quality_errors を出さない（誤検出しない）。
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    PILImage.new("RGB", (128, 128), (220, 60, 40)).save(export_dir / "good.png")
    panel = _quality_panel(image_asset="good.png")
    manga = _quality_manga(panel)
    status = build_production_status("proj", manga, export_dir)
    codes = {issue.code for issue in status.quality_errors}
    assert "empty_panel_image" not in codes


# --- box format: xyxy入力→xywh正規化→永続化の往復が壊れない ---


def test_script_panel_xyxy_box_format_round_trips() -> None:
    panel = ScriptPanel.model_validate(
        {
            "shot": "t",
            "text_safe_area_format": "xyxy",
            "text_safe_area": [0.65, 0.72, 0.92, 0.88],
            "character_directives": [
                {"name": "美嘉", "region_box_format": "xyxy", "region_box": [0.1, 0.2, 0.5, 0.7]}
            ],
        }
    )
    assert panel.text_safe_area == pytest.approx((0.65, 0.72, 0.27, 0.16))
    assert panel.text_safe_area_format == "xywh"
    directive = panel.character_directives[0]
    assert directive.region_box == pytest.approx((0.1, 0.2, 0.4, 0.5))
    assert directive.region_box_format == "xywh"

    # model_dump → 再validateで二重変換されない。
    reloaded = ScriptPanel.model_validate(panel.model_dump())
    assert reloaded.text_safe_area == pytest.approx((0.65, 0.72, 0.27, 0.16))
    assert reloaded.character_directives[0].region_box == pytest.approx((0.1, 0.2, 0.4, 0.5))


# --- 無言コマ検査 ---


def _silent_project(panels: list[Panel]) -> tuple[MangaProject, Page]:
    page = Page(page=1, theme="t", layout_template="o", panels=panels)
    manga = MangaProject(
        title="t",
        characters=[Character(id="a", display_name="A", trigger_prompt="a")],
        pages=[page],
    )
    return manga, page


def _char_panel(panel_id: str, *, dialogue=None, role: str = "dialogue") -> Panel:
    return Panel(
        panel_id=panel_id,
        bbox=(0.0, 0.0, 1.0, 1.0),
        shot="medium",
        role=role,
        characters=["a"],
        dialogue=dialogue or [],
    )


def test_too_many_silent_panels_warns() -> None:
    manga, page = _silent_project(
        [_char_panel(f"p{i}") for i in range(1, 5)]  # 人物コマ4枚すべて無言
    )
    issues = preflight.preflight_page(manga, page, 0)
    silent = [issue for issue in issues if issue.code == "too_many_silent_panels"]
    assert len(silent) == 1
    assert silent[0].level == "warning"


def test_silent_panels_not_warned_when_dialogue_present() -> None:
    panels = [
        _char_panel("p1", dialogue=[Dialogue(speaker="a", text="やあ")]),
        _char_panel("p2", dialogue=[Dialogue(speaker="a", text="うん")]),
        _char_panel("p3", dialogue=[Dialogue(speaker="a", text="そう")]),
        _char_panel("p4"),
    ]
    manga, page = _silent_project(panels)
    issues = preflight.preflight_page(manga, page, 0)
    assert not any(issue.code == "too_many_silent_panels" for issue in issues)


def test_intentional_silent_roles_not_warned() -> None:
    panels = [_char_panel(f"p{i}", role="silent") for i in range(1, 5)]
    manga, page = _silent_project(panels)
    issues = preflight.preflight_page(manga, page, 0)
    assert not any(issue.code == "too_many_silent_panels" for issue in issues)
