import { describe, expect, it } from "vitest";
import {
  bboxFromFramePoints,
  panelFramePoints,
  rectFramePoints,
  transformFramePoints
} from "./page-editor-helpers";

describe("ページ編集ヘルパー", () => {
  it("矩形bboxを4点polygonへ変換する", () => {
    expect(rectFramePoints([0.1, 0.2, 0.3, 0.4])).toEqual([
      [0.1, 0.2],
      [0.4, 0.2],
      [0.4, 0.6000000000000001],
      [0.1, 0.6000000000000001]
    ]);
  });

  it("frame_pointsを正本にし、旧shape_pointsはbbox相対からページ座標へ移す", () => {
    expect(
      panelFramePoints({
        bbox: [0.2, 0.2, 0.4, 0.4],
        shape_points: [
          [0, 0],
          [1, 0],
          [0.5, 1]
        ]
      })
    ).toEqual([
      [0.2, 0.2],
      [0.6000000000000001, 0.2],
      [0.4, 0.6000000000000001]
    ]);
    expect(
      panelFramePoints({
        bbox: [0, 0, 1, 1],
        frame_points: [
          [0.1, 0.1],
          [0.9, 0.1],
          [0.9, 0.9],
          [0.1, 0.9]
        ]
      })[0]
    ).toEqual([0.1, 0.1]);
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
