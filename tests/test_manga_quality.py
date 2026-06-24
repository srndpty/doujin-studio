"""4ページ漫画の商用品質改善（領域1〜5）のテスト。"""

from __future__ import annotations

from backend.app import preflight, renderer, story
from backend.app.generator import suggest_candidate_count
from backend.app.renderer import _panel_box_px, compute_bubble_layout
from backend.app.schemas import (
    Character,
    Dialogue,
    MangaProject,
    Page,
    Panel,
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


def test_offscreen_dialogue_disables_tail() -> None:
    line = Dialogue(speaker="a", text="画面外の声", on_screen=False)
    assert line.tail is not None and line.tail.enabled is False


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


def test_offscreen_narration_speaker_not_drawn() -> None:
    base = MangaProject(
        title="t",
        characters=[Character(id="mika", display_name="美嘉", trigger_prompt="mika 1girl")],
    )
    script = ScriptStage(
        pages=[
            ScriptPage(
                page=page,
                panels=[
                    ScriptPanel(
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
                ],
            )
            for page in range(1, 5)
        ]
    )
    manga = story.script_to_manga(script, base)
    panel = manga.pages[0].panels[0]
    # ナレーション話者は描画人物に含めない（画面外台詞を許可）。
    assert panel.characters == []
    assert panel.dialogue[0].kind == "narration"
    assert panel.dialogue[0].on_screen is False


# --- 領域2: 生成後の編集チェック ---


def test_review_script_moves_onomatopoeia_and_removes_misused_sfx() -> None:
    data = {
        "pages": [
            {
                "page": 1,
                "panels": [
                    {
                        "characters": ["美嘉"],
                        "dialogue": [
                            {"speaker": "美嘉", "text": "ドカーン", "kind": "speech"},
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
    assert "ドカーン" not in texts  # 擬音は台詞から除去
    assert any(s["text"] == "ドカーン" for s in panel["sfx"])  # 擬音欄へ移動
    monologue = next(line for line in panel["dialogue"] if line["text"] == "（やってしまった）")
    assert monologue["kind"] == "monologue"
    shout = next(line for line in panel["dialogue"] if line["text"] == "やめろ！！")
    assert shout["kind"] == "shout"
    assert all(s["text"] != "トドメ" for s in panel["sfx"])  # 場面非対応の擬音は削除
    assert warnings


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
