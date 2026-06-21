import { describe, expect, it } from "vitest";
import type { components } from "./api/schema";
import { composePromptPreview, mergePromptParts } from "./prompt-preview";

type MangaProject = components["schemas"]["MangaProject"];
type Panel = components["schemas"]["Panel"];

describe("mergePromptParts", () => {
  it("カンマ区切りを正規化し重複を大文字小文字無視で除去する", () => {
    expect(mergePromptParts(["a, B", "b , c", " A "])).toBe("a, B, c");
  });

  it("空文字や空白だけの要素を除外する", () => {
    expect(mergePromptParts(["", "  ", "x"])).toBe("x");
  });
});

describe("composePromptPreview", () => {
  const manga = {
    title: "t",
    common_positive_prompt: "masterpiece",
    common_negative_prompt: "worst quality",
    characters: [
      {
        id: "char_a",
        display_name: "春香",
        trigger_prompt: "haruka",
        appearance_prompt: "blue eyes",
        outfit_prompt: "school uniform",
        negative_prompt: "bad hands"
      }
    ]
  } as unknown as MangaProject;

  it("コマのキャラと共通プロンプトを統合し重複を除く", () => {
    const panel = {
      panel_id: "p01_01",
      characters: ["char_a"],
      prompt: "fallback",
      generation: { prompt: "smiling, masterpiece", negative_prompt: "blurry" }
    } as unknown as Panel;

    const result = composePromptPreview(manga, panel);
    expect(result.positive).toBe("masterpiece, haruka, blue eyes, school uniform, smiling");
    expect(result.negative).toBe("worst quality, bad hands, blurry");
  });

  it("未知のキャラ参照は無視し、generation.promptが空ならpanel.promptを使う", () => {
    const panel = {
      panel_id: "p01_02",
      characters: ["missing"],
      prompt: "scene prompt",
      generation: { prompt: "", negative_prompt: "" }
    } as unknown as Panel;

    const result = composePromptPreview(manga, panel);
    expect(result.positive).toBe("masterpiece, scene prompt");
    expect(result.negative).toBe("worst quality");
  });
});
