from __future__ import annotations

import math

from .schemas import (
    Character,
    Dialogue,
    GenerationInfo,
    LocationProfile,
    MangaProject,
    Page,
    Panel,
    Sfx,
    WorkflowPreset,
)

# 1ページの描画サイズ（rendererと一致させる）。
PAGE_SIZE = (1200, 1700)


def compute_generation_size(
    bbox: tuple[float, float, float, float],
    base_pixels: int = 1024 * 1024,
    multiple: int = 64,
    min_dim: int = 512,
    max_dim: int = 1536,
) -> tuple[int, int]:
    """コマの縦横比からComfyUI生成サイズを求め、64px単位へ丸める。

    生成画像の比率をコマへ近づけ、はめ込み時の強制cropを減らす。
    """
    page_w, page_h = PAGE_SIZE
    panel_w = max(1.0, bbox[2] * page_w)
    panel_h = max(1.0, bbox[3] * page_h)
    aspect = panel_w / panel_h
    height = math.sqrt(base_pixels / aspect)
    width = aspect * height

    def snap(value: float) -> int:
        rounded = int(round(value / multiple)) * multiple
        return max(min_dim, min(max_dim, rounded))

    return snap(width), snap(height)


DEFAULT_COMMON_POSITIVE_PROMPT = "masterpiece, best quality, score_7, safe, anime"
DEFAULT_COMMON_NEGATIVE_PROMPT = (
    "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia, "
    "bad hands, bad anatomy, extra fingers, missing fingers, text, watermark, speech bubble, logo"
)

LAYOUTS = {
    "vertical_3_start": [
        (0.06, 0.05, 0.88, 0.28),
        (0.06, 0.36, 0.42, 0.27),
        (0.52, 0.36, 0.42, 0.27),
        (0.06, 0.67, 0.88, 0.27),
    ],
    "conversation_4": [
        (0.06, 0.05, 0.42, 0.28),
        (0.52, 0.05, 0.42, 0.28),
        (0.06, 0.37, 0.88, 0.25),
        (0.06, 0.67, 0.88, 0.27),
    ],
    "reaction_3": [
        (0.06, 0.05, 0.88, 0.30),
        (0.06, 0.40, 0.88, 0.22),
        (0.06, 0.67, 0.88, 0.27),
    ],
    "punchline_3": [
        (0.06, 0.05, 0.42, 0.30),
        (0.52, 0.05, 0.42, 0.30),
        (0.06, 0.40, 0.88, 0.54),
    ],
}


def generate_four_page_name(
    title: str,
    work_name: str,
    character_a: str,
    character_b: str,
    situation: str,
    ending_direction: str,
    target_pages: int = 4,
) -> MangaProject:
    char_a = Character(
        id="char_a",
        display_name=character_a,
        role="主役",
        speech_style="短めで反応が分かりやすい口調",
        visual_notes="表情差分を重視",
        trigger_prompt=character_a,
        appearance_prompt="consistent face, consistent hairstyle",
        outfit_prompt="consistent outfit",
        negative_prompt="inconsistent character design",
    )
    char_b = Character(
        id="char_b",
        display_name=character_b,
        role="相方",
        speech_style="状況を動かす台詞を担当",
        visual_notes="立ち位置が分かる構図を重視",
        trigger_prompt=character_b,
        appearance_prompt="consistent face, consistent hairstyle",
        outfit_prompt="consistent outfit",
        negative_prompt="inconsistent character design",
    )

    page_specs = [
        (
            1,
            "導入。状況と二人の目的を見せる",
            "vertical_3_start",
            "establishing shot, after school room, soft daylight, calm mood",
            (1024, 640, "cover", "center"),
            [
                (
                    "状況提示のロングショット",
                    "少し高い視点",
                    [character_a, character_b],
                    f"{situation}。二人が並んで状況を確認している。",
                    [(character_a, "これ、思ったより大ごとじゃない？")],
                ),
                (
                    "バストアップ",
                    "正面",
                    [character_b],
                    "相方が妙に自信ありげに話す。",
                    [(character_b, "大丈夫。段取りは完璧だから。")],
                ),
                (
                    "顔アップ",
                    "寄り",
                    [character_a],
                    "主役が不安そうに目を細める。",
                    [(character_a, "その言い方が一番こわいんだけど。")],
                ),
                (
                    "二人のリアクション",
                    "水平",
                    [character_a, character_b],
                    "小さな違和感に気づく。",
                    [],
                ),
            ],
        ),
        (
            2,
            "展開。誤解やすれ違いが大きくなる",
            "conversation_4",
            "two character conversation, expressive faces, medium shot, clean background",
            (896, 640, "cover", "center"),
            [
                (
                    "会話コマ",
                    "左寄り",
                    [character_a],
                    "主役が確認する。",
                    [(character_a, "念のため聞くけど、何を準備したの？")],
                ),
                (
                    "会話コマ",
                    "右寄り",
                    [character_b],
                    "相方がさらっと答える。",
                    [(character_b, "勢いで乗り切るための勢い。")],
                ),
                (
                    "沈黙コマ",
                    "固定",
                    [character_a, character_b],
                    "二人の間に長い沈黙が落ちる。",
                    [],
                ),
                (
                    "ツッコミ",
                    "寄り",
                    [character_a, character_b],
                    "主役が一気にツッコむ。",
                    [(character_a, "それ準備って言わない！")],
                ),
            ],
        ),
        (
            3,
            "転換。失敗しかけた状況から別の意味が出る",
            "reaction_3",
            "dynamic reaction, comedic timing, energetic pose, manga composition",
            (896, 672, "cover", "top"),
            [
                (
                    "動きのあるコマ",
                    "斜め",
                    [character_a, character_b],
                    "二人が慌てて立て直す。",
                    [(character_b, "でも、今なら逆にいけるかも。")],
                ),
                (
                    "顔アップ",
                    "寄り",
                    [character_a],
                    "主役が意図に気づく。",
                    [(character_a, "逆にって何をどう逆に？")],
                ),
                (
                    "大きめリアクション",
                    "低め",
                    [character_a, character_b],
                    "場の空気が少しだけ好転する。",
                    [(character_b, "ほら、結果的に注目された。")],
                ),
            ],
        ),
        (
            4,
            "オチ。指定された方向で短く締める",
            "punchline_3",
            "punchline scene, comedic contrast, clear silhouettes, final panel emphasis",
            (1024, 768, "cover", "center"),
            [
                (
                    "確認",
                    "正面",
                    [character_a],
                    "主役が最後の確認をする。",
                    [(character_a, "つまり成功ってことでいいの？")],
                ),
                (
                    "自信満々",
                    "正面",
                    [character_b],
                    "相方が胸を張る。",
                    [(character_b, "もちろん。予定通りだよ。")],
                ),
                (
                    "オチの大ゴマ",
                    "引き",
                    [character_a, character_b],
                    f"{ending_direction}。二人の温度差で締める。",
                    [(character_a, "絶対いま予定って言葉を作ったでしょ。")],
                ),
            ],
        ),
    ]

    pages: list[Page] = []
    for page_number, theme, template_id, page_prompt, page_settings, panels in page_specs:
        width, height, fit_mode, crop_anchor = page_settings
        page_panels: list[Panel] = []
        for index, (shot, camera, names, prompt, dialogue_specs) in enumerate(panels, start=1):
            panel_id = f"p{page_number:02d}_{index:02d}"
            character_ids = ["char_a" if name == character_a else "char_b" for name in names]
            dialogues = [
                Dialogue(
                    speaker="char_a" if speaker == character_a else "char_b",
                    text=text,
                    position="upper_right" if i % 2 == 0 else "upper_left",
                )
                for i, (speaker, text) in enumerate(dialogue_specs)
            ]
            panel_bbox = LAYOUTS[template_id][index - 1]
            gen_w, gen_h = compute_generation_size(panel_bbox)
            subject_mode = (
                "reaction" if ("リアクション" in shot or "沈黙" in shot) else "character_scene"
            )
            page_panels.append(
                Panel(
                    panel_id=panel_id,
                    bbox=panel_bbox,
                    shot=shot,
                    subject_mode=subject_mode,
                    camera=camera,
                    location_id="default_room",
                    characters=character_ids,
                    prompt=f"{work_name}, {situation}, {prompt}",
                    dialogue=dialogues,
                    sfx=[Sfx(text="しーん", position="center")] if "沈黙" in shot else [],
                    generation=GenerationInfo(
                        backend="stub",
                        prompt=f"{page_prompt}, {work_name}, {situation}, {prompt}",
                        negative_prompt=DEFAULT_COMMON_NEGATIVE_PROMPT,
                        seed=page_number * 100 + index,
                        width=gen_w,
                        height=gen_h,
                        fit_mode=fit_mode,
                        crop_anchor=crop_anchor,
                        status="pending",
                    ),
                )
            )
        pages.append(
            Page(page=page_number, theme=theme, layout_template=template_id, panels=page_panels)
        )

    manga = MangaProject(
        title=title,
        work_name=work_name,
        premise=f"{situation}から始まり、{ending_direction}で締める4ページ短編。",
        target_pages=target_pages,
        common_positive_prompt=DEFAULT_COMMON_POSITIVE_PROMPT,
        common_negative_prompt=DEFAULT_COMMON_NEGATIVE_PROMPT,
        characters=[char_a, char_b],
        locations=[
            LocationProfile(
                id="default_room",
                display_name="基本の部屋",
                prompt="consistent room layout, consistent background",
                negative_prompt="inconsistent background, changing room layout",
            )
        ],
        workflow_presets=[WorkflowPreset(id="default", name="workflow既定")],
        active_workflow_preset_id="default",
        pages=pages,
    )
    if target_pages > 4:
        expand_pages(manga, target_pages)
    return manga


def expand_pages(manga: MangaProject, target_pages: int) -> None:
    base_pages = [page.model_copy(deep=True) for page in manga.pages]
    for page_number in range(5, target_pages + 1):
        page = base_pages[(page_number - 1) % len(base_pages)].model_copy(deep=True)
        page.page = page_number
        page.theme = f"{page_number}ページ目。{page.theme}"
        page.render_status = "pending"
        page.rendered_at = None
        for index, panel in enumerate(page.panels, start=1):
            panel.panel_id = f"p{page_number:02d}_{index:02d}"
            panel.generation.seed = page_number * 100 + index
            panel.generation.prompt = (
                f"story continuation, page {page_number}, {panel.generation.prompt}"
            )
            panel.image_asset = None
            panel.image_candidates = []
            panel.selected_candidate_id = None
        manga.pages.append(page)
