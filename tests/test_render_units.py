"""写植エンジン(typeset)とフォント解決(fonts)の単体テスト。

縦書き・横書きの描画経路と各セル種別（縦中横・回転・句読点・小書き仮名）、
禁則処理、フォントローダを網羅し、レンダリングの変更行を検証する。
"""

from __future__ import annotations

from PIL import Image

from backend.app import fonts, typeset


def test_vertical_layout_and_draw_covers_all_cell_kinds() -> None:
    # 縦中横(ABC123)・回転(ー)・波ダッシュ反転(～)・三点リーダ中央寄せ(…)・
    # 句読点(、。)・小書き仮名(っ)・改行・括弧を含め、各描画分岐を通す。
    text = "テスト、ABC123ー…っ。～\n次の「行」だ"
    layout = typeset.layout_text(text, None, 240, 360, vertical=True, default_size=30, min_size=20)
    assert layout.vertical is True
    assert layout.columns
    image = Image.new("RGBA", (400, 500), (0, 0, 0, 0))
    # noneバルーン相当の縁取り(stroke_width>0)経路も通す。
    typeset.draw_layout(image, layout, None, (10, 10, 250, 370), (10, 10, 10), stroke_width=3)
    # 全文字が保持される（切り捨てない）。
    flattened = "".join(token[1] for line in layout.columns for token in line)
    for ch in "テストABC123ー…～次の行だ":
        assert ch in flattened


def test_vertical_rotates_wave_and_dash_variants() -> None:
    # 縦書きで横倒しになりがちな波ダッシュ・チルダ・長ダッシュ類が回転トークンになること。
    # 「お疲れ様～！」のような全角チルダ(U+FF5E)が横向きのまま残る不具合の回帰防止。
    for ch in "〜～⁓―—－ー":
        tokens = typeset.tokenize_vertical(f"あ{ch}い")
        kinds = {token[0] for token in tokens if token[1] == ch}
        assert kinds == {"rot"}, f"{ch!r}(U+{ord(ch):04X}) が回転対象になっていない"


def test_vertical_punctuation_is_drawn_at_cell_top_right() -> None:
    # 縦書きの読点はセル右上へ。フォントによりbboxが全角幅で返っても左上へ流れないこと。
    fp = fonts.find_dialogue_font_path("")
    if fp is None:
        return  # 日本語フォント未導入の環境ではスキップ
    layout = typeset.layout_text(
        "あ、", str(fp), 60, 140, vertical=True, default_size=40, min_size=40
    )
    width, height = 160, 200
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    typeset.draw_layout(image, layout, str(fp), (10, 10, width - 10, height - 10), (0, 0, 0))
    pixels = image.convert("L").load()
    cell = layout.cell

    def ink_x_range(y0: int, y1: int) -> tuple[int, int]:
        xs = [x for y in range(y0, y1) for x in range(width) if pixels[x, y] < 128]
        return (min(xs), max(xs)) if xs else (0, 0)

    base_min, base_max = ink_x_range(10, int(10 + cell))  # 「あ」セル
    comma_min, comma_max = ink_x_range(int(10 + cell), int(10 + cell * 2))  # 「、」セル
    base_center = (base_min + base_max) / 2
    comma_center = (comma_min + comma_max) / 2
    # 読点のインク中心が本文字より右側にあること（右上配置）。
    assert comma_center > base_center, (base_center, comma_center)


def test_horizontal_layout_and_draw() -> None:
    layout = typeset.layout_text(
        "Hello world テスト", None, 360, 120, vertical=False, default_size=28, min_size=18
    )
    assert layout.vertical is False
    image = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    typeset.draw_layout(image, layout, None, (10, 10, 370, 130), (10, 10, 10))


def test_kinsoku_moves_opening_bracket_off_line_end() -> None:
    # 行末禁則: 開き括弧が列末に来ないよう次列へ送られる。
    tokens = typeset.tokenize_vertical("あい「うえ")
    lines = typeset.wrap_tokens(tokens, 3)
    for line in lines[:-1]:
        assert line[-1][1] not in typeset.LINE_END_FORBIDDEN


def test_line_start_forbidden_is_pulled_up() -> None:
    # 行頭禁則: 句点が列頭に来ないよう前の列へ追い込む。
    tokens = typeset.tokenize_vertical("ああ。い")
    lines = typeset.wrap_tokens(tokens, 2)
    for line in lines:
        assert line[0][1] not in typeset.LINE_START_FORBIDDEN


def test_font_loaders_and_listing() -> None:
    assert fonts.load_dialogue_font(24) is not None
    assert fonts.load_label_font(24, bold=True) is not None
    assert fonts.load_label_font(24, bold=False) is not None
    listed = {item["id"] for item in fonts.list_fonts()}
    assert listed >= {"genei_antique", "biz_ud_gothic", "yu_gothic", "ms_gothic"}


def test_scan_for_keywords_returns_none_for_unknown_font() -> None:
    assert fonts._scan_for_keywords(["definitely-not-an-installed-font-xyz"]) is None
