import { describe, expect, it } from "vitest";
import {
  bboxFromFramePoints,
  shapePointsForPreset,
  shapePreset,
  transformFramePoints
} from "./page-editor-helpers";

describe("ページ編集ヘルパー", () => {
  it("プリセット形状を相互変換する", () => {
    const slantRight = shapePointsForPreset("slant-right");
    expect(slantRight).not.toBeNull();
    expect(shapePreset(slantRight)).toBe("slant-right");
    expect(shapePreset(shapePointsForPreset("slant-left"))).toBe("slant-left");
    expect(shapePreset(null)).toBe("rectangle");
  });

  it("frame pointsからページ内bboxを計算する", () => {
    const result = bboxFromFramePoints([
      [-0.2, 0.2],
      [0.8, 0.1],
      [1.2, 0.9],
      [0.1, 1.1]
    ]);
    expect(result[0]).toBeCloseTo(0);
    expect(result[1]).toBeCloseTo(0.1);
    expect(result[2]).toBeCloseTo(1);
    expect(result[3]).toBeCloseTo(0.9);
  });

  it("変形コマの点を裁ち落とし境界内に丸める", () => {
    const transformed = transformFramePoints(
      [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1]
      ],
      [0, 0, 1, 1],
      [-0.2, 0.33, 1.4, 0.8]
    );
    expect(transformed).toEqual([
      [-0.05, 0.33],
      [1.05, 0.33],
      [1.05, 1.05],
      [-0.05, 1.05]
    ]);
  });
});
