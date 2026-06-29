"""生成prompt（positive）の正規化。

画像生成モデル向けのpositive promptは自然文よりbooruタグを優先する。``white``,
``blank``, ``empty space`` のような単独指定は被写体のない白紙コマを誘発しやすいため
除去し、``white background`` のような背景指定は被写体維持を明示する背景タグへ寄せて
意図（クリーンな背景）を保ちつつ白飛びを避ける。

除去・置換はカンマ区切りタグ単位の完全一致で行う。``white hair`` や ``white shirt``
のような複合タグの ``white`` は対象にしない（白紙化を招くのは単独指定のため）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 単独で指定すると白紙・余白過多を誘発しやすいタグ（カンマ区切りで完全一致のものを除去）。
BLANK_RISK_TAGS: frozenset[str] = frozenset(
    {
        "white",
        "blank",
        "empty",
        "empty space",
        "emptiness",
        "void",
        "nothing",
        "plain",
        "plain white",
        "white space",
        "whitespace",
        "negative space",
        "copy space",
        "pure white",
        "all white",
    }
)

# 曖昧な背景指定 → booruタグへの寄せ替え。被写体を消さず意図（背景指定）を残す。
SAFE_SIMPLE_BACKGROUND = "simple background, visible subject, clear foreground"
BOORU_REPLACEMENTS: dict[str, str] = {
    "white background": SAFE_SIMPLE_BACKGROUND,
    "empty background": SAFE_SIMPLE_BACKGROUND,
    "plain background": SAFE_SIMPLE_BACKGROUND,
    "blank background": SAFE_SIMPLE_BACKGROUND,
    "no background": SAFE_SIMPLE_BACKGROUND,
    "solid white background": SAFE_SIMPLE_BACKGROUND,
}


@dataclass
class PromptNormalization:
    prompt: str
    removed: list[str] = field(default_factory=list)
    replaced: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.replaced)


def _split_tags(prompt: str) -> list[str]:
    return [tag.strip() for tag in prompt.split(",")]


def normalize_prompt(prompt: str) -> PromptNormalization:
    """白紙誘発タグを除去し、曖昧な背景指定をbooruタグへ寄せる。

    タグの並び順は保持し、重複除去は呼び出し側のmerge_prompt_partsへ委ねる。
    """
    result_tags: list[str] = []
    removed: list[str] = []
    replaced: list[tuple[str, str]] = []
    for tag in _split_tags(prompt):
        if not tag:
            continue
        key = tag.casefold()
        if key in BLANK_RISK_TAGS:
            removed.append(tag)
            continue
        replacement = BOORU_REPLACEMENTS.get(key)
        if replacement is not None:
            replaced.append((tag, replacement))
            result_tags.append(replacement)
            continue
        result_tags.append(tag)
    return PromptNormalization(prompt=", ".join(result_tags), removed=removed, replaced=replaced)


def blank_risk_tags(prompt: str) -> list[str]:
    """白紙誘発の原因になりうるタグ（除去・置換対象）を抽出する。preflight検査用。"""
    hits: list[str] = []
    for tag in _split_tags(prompt):
        if not tag:
            continue
        key = tag.casefold()
        if key in BLANK_RISK_TAGS or key in BOORU_REPLACEMENTS:
            hits.append(tag)
    return hits
