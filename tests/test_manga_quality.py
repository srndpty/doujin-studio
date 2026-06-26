"""4ページ漫画の商用品質改善（領域1〜5）のテスト。"""

from __future__ import annotations

import pytest

from backend.app import preflight, renderer, story
from backend.app.generator import suggest_candidate_count
from backend.app.prompt_composer import compose_panel_prompts
from backend.app.renderer import _panel_box_px, compute_bubble_layout
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
    assert Dialogue(speaker="a", text="…", kind="monologue").balloon == "cloud"
    assert Dialogue(speaker="a", text="その時", kind="narration").balloon == "caption"
    assert Dialogue(speaker="a", text="うわっ", kind="shout").balloon == "burst"
    # 明示的に指定したballoonは尊重する（kindに上書きされない）。
    assert Dialogue(speaker="a", text="…", kind="monologue", balloon="caption").balloon == "caption"
    # ovalを明示指定した独白は、cloudへ書き換えずovalのまま保持する。
    assert Dialogue(speaker="a", text="……", kind="monologue", balloon="oval").balloon == "oval"


def test_saved_json_kind_change_updates_balloon() -> None:
    # 保存済みJSON（balloonが常に含まれる）の再編集でkindを変えると、既定形状へ追従する。
    payload = Dialogue(speaker="a", text="本文").model_dump()
    assert payload["balloon"] == "oval" and payload["balloon_auto"] is True
    payload["kind"] = "narration"
    assert Dialogue.model_validate(payload).balloon == "caption"
    # 明示指定（balloon_auto=False）は再編集でも保持される。
    manual = Dialogue(speaker="a", text="本文", kind="monologue", balloon="oval").model_dump()
    assert manual["balloon_auto"] is False
    manual["kind"] = "narration"
    assert Dialogue.model_validate(manual).balloon == "oval"


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
    assert layout["rika"].expression == "crying" and layout["rika"].position == "lower_right"
    positive, _negative = compose_panel_prompts(manga, drawn)
    for token in ("smiling", "waving hand", "crying", "on the upper left", "on the lower right"):
        assert token in positive


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
