import type { components } from "./api/schema";

// App.tsxから切り出した純粋ロジック。単体テスト可能にする。
// 手書きMangaProject（座標タプル）とOpenAPI生成型のどちらも渡せるよう、
// 必要なフィールドだけを構造的に受け取る。
type PromptCharacter = Pick<
  components["schemas"]["Character"],
  "id" | "display_name" | "trigger_prompt" | "appearance_prompt" | "outfit_prompt" | "negative_prompt"
>;
type MangaProject = {
  common_positive_prompt: string;
  common_negative_prompt: string;
  characters?: PromptCharacter[];
};
type Panel = {
  characters?: string[];
  prompt: string;
  generation?: { prompt?: string; negative_prompt?: string };
};

export function mergePromptParts(parts: string[]): string {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const part of parts) {
    for (const rawTag of part.split(",")) {
      const tag = rawTag.trim();
      const key = tag.toLocaleLowerCase();
      if (tag && !seen.has(key)) {
        tags.push(tag);
        seen.add(key);
      }
    }
  }
  return tags.join(", ");
}

export function composePromptPreview(
  manga: MangaProject,
  panel: Panel
): { positive: string; negative: string } {
  const characterMap = new Map((manga.characters ?? []).map((character) => [character.id, character]));
  const positive = [manga.common_positive_prompt];
  const negative = [manga.common_negative_prompt];
  for (const characterId of panel.characters ?? []) {
    const character = characterMap.get(characterId);
    if (!character) continue;
    positive.push(
      character.trigger_prompt || character.display_name,
      character.appearance_prompt ?? "",
      character.outfit_prompt ?? ""
    );
    negative.push(character.negative_prompt ?? "");
  }
  positive.push(panel.generation?.prompt || panel.prompt);
  negative.push(panel.generation?.negative_prompt ?? "");
  return { positive: mergePromptParts(positive), negative: mergePromptParts(negative) };
}
