"""preflightのfixableな品質問題を自動修正する。

mutate関数の中でMangaProjectを書き換える。決定的に直せる問題だけを対象にし、適用した
修正の説明文（日本語）を返す。対象は領域: 品質ゲートで``fixable=True``として検出する：

- prompt_blank_risk: panel.prompt/composition_notesから白紙誘発語を除去しbooru寄せ
- monologue_cloud_balloon: 独白の丸泡(cloud)を矩形キャプション(caption)へ
- sfx_english_text: 辞書にある英字擬音を日本語写植へ
- tail_not_pointing_to_speaker: しっぽ先端を話者領域中心へ寄せる
- subject_too_small: 画像検査で小被写体と判定されたコマのcropを拡大する
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .preflight import TAIL_SPEAKER_MAX_DISTANCE, _speaker_center
from .prompt_normalizer import normalize_prompt
from .schemas import MAX_CROP_SCALE, MangaProject, Page, PreflightIssue
from .story import normalize_sfx_text


@dataclass(frozen=True)
class AutofixChange:
    page: int
    panel_id: str | None
    code: str
    message: str


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _fix_prompt_blank_risk(page: Page) -> list[AutofixChange]:
    changes: list[AutofixChange] = []
    for panel in page.panels:
        applied: list[str] = []
        prompt_result = normalize_prompt(panel.prompt)
        if prompt_result.changed:
            panel.prompt = prompt_result.prompt
            applied.extend(prompt_result.removed)
            applied.extend(before for before, _after in prompt_result.replaced)
        notes_result = normalize_prompt(panel.composition_notes)
        if notes_result.changed:
            panel.composition_notes = notes_result.prompt
            applied.extend(notes_result.removed)
            applied.extend(before for before, _after in notes_result.replaced)
        generation_result = normalize_prompt(panel.generation.prompt)
        if generation_result.changed:
            panel.generation.prompt = generation_result.prompt
            applied.extend(generation_result.removed)
            applied.extend(before for before, _after in generation_result.replaced)
        if applied:
            unique = list(dict.fromkeys(applied))
            changes.append(
                AutofixChange(
                    page=page.page,
                    panel_id=panel.panel_id,
                    code="prompt_blank_risk",
                    message=f"{page.page}ページ {panel.panel_id}: 白紙誘発語を整理（{', '.join(unique)}）",
                )
            )
    return changes


def _fix_monologue_balloon(page: Page) -> list[AutofixChange]:
    changes: list[AutofixChange] = []
    for panel in page.panels:
        for dialogue in panel.dialogue:
            if dialogue.kind == "monologue" and dialogue.balloon == "cloud":
                dialogue.balloon = "caption"
                changes.append(
                    AutofixChange(
                        page=page.page,
                        panel_id=panel.panel_id,
                        code="monologue_cloud_balloon",
                        message=f"{page.page}ページ {panel.panel_id}: 独白の吹き出しをキャプションへ変更",
                    )
                )
    return changes


def _fix_sfx_language(page: Page) -> list[AutofixChange]:
    changes: list[AutofixChange] = []
    for panel in page.panels:
        for sfx in panel.sfx:
            normalized = normalize_sfx_text(sfx.text)
            if normalized != sfx.text:
                changes.append(
                    AutofixChange(
                        page=page.page,
                        panel_id=panel.panel_id,
                        code="sfx_english_text",
                        message=f"{page.page}ページ {panel.panel_id}: 擬音「{sfx.text}」を「{normalized}」へ",
                    )
                )
                sfx.text = normalized
    return changes


def _fix_balloon_tails(page: Page) -> list[AutofixChange]:
    changes: list[AutofixChange] = []
    for panel in page.panels:
        for dialogue in panel.dialogue:
            if not dialogue.on_screen or dialogue.balloon in {"caption", "none"}:
                continue
            tail = dialogue.tail
            if tail is None or not tail.enabled:
                continue
            center = _speaker_center(dialogue, panel)
            if center is None:
                continue
            distance = math.hypot(tail.tip[0] - center[0], tail.tip[1] - center[1])
            if distance > TAIL_SPEAKER_MAX_DISTANCE:
                tail.tip = (_clamp_unit(center[0]), _clamp_unit(center[1]))
                changes.append(
                    AutofixChange(
                        page=page.page,
                        panel_id=panel.panel_id,
                        code="tail_not_pointing_to_speaker",
                        message=f"{page.page}ページ {panel.panel_id}: しっぽを話者（{dialogue.speaker}）へ向け直し",
                    )
                )
    return changes


def _fix_subject_too_small(page: Page, issues: list[PreflightIssue] | None) -> list[AutofixChange]:
    """画像メトリクス由来の小被写体警告だけ、crop拡大で決定的に改善する。"""
    if issues is None:
        return []
    target_ids = {
        issue.panel_id
        for issue in issues
        if issue.page == page.page and issue.panel_id and issue.code == "subject_too_small"
    }
    changes: list[AutofixChange] = []
    for panel in page.panels:
        if panel.panel_id not in target_ids:
            continue
        before = panel.generation.crop_scale
        after = min(MAX_CROP_SCALE, max(1.35, before * 1.25))
        if after <= before:
            continue
        # crop倍率だけ上げ、利用者が調整した注視点(offset/focal)は維持する。被写体が端に
        # あるコマでoffsetを0へ戻すと拡大で被写体を画面外へ追い出すため（領域5）。
        # offset/focalの許容範囲はscaleに依存しない(-1..1 / 0..1)ためclampは不要。
        panel.generation.crop_scale = after
        changes.append(
            AutofixChange(
                page=page.page,
                panel_id=panel.panel_id,
                code="subject_too_small",
                message=f"{page.page}ページ {panel.panel_id}: 被写体を大きく見せるためcropを拡大",
            )
        )
    return changes


def autofix_page(page: Page, issues: list[PreflightIssue] | None = None) -> list[AutofixChange]:
    """1ページ分のfixableな問題を修正し、適用した修正の説明を返す。"""
    changes: list[AutofixChange] = []
    changes.extend(_fix_prompt_blank_risk(page))
    changes.extend(_fix_monologue_balloon(page))
    changes.extend(_fix_sfx_language(page))
    changes.extend(_fix_balloon_tails(page))
    changes.extend(_fix_subject_too_small(page, issues))
    return changes


def autofix_manga(
    manga: MangaProject,
    page_number: int | None = None,
    issues: list[PreflightIssue] | None = None,
) -> list[AutofixChange]:
    """fixableな問題を自動修正する。page_number指定時はそのページのみ対象にする。"""
    changes: list[AutofixChange] = []
    for page in manga.pages:
        if page_number is not None and page.page != page_number:
            continue
        changes.extend(autofix_page(page, issues))
    return changes
