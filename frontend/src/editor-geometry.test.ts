import { describe, expect, it } from "vitest";
import { computeImagePlacement, normalizeBox, overlapsWithGutter } from "./editor-geometry";

describe("ページ編集の座標制約", () => {
  it("コマをページ内へ収め、最小サイズを保証する", () => {
    const [x, y, width, height] = normalizeBox([0.98, -0.3, 0.001, 1.4]);
    expect(width).toBeGreaterThanOrEqual(0.04);
    expect(height).toBeLessThanOrEqual(1);
    expect(x + width).toBeLessThanOrEqual(1);
    expect(y).toBeGreaterThanOrEqual(0);
  });

  it("ガターを挟んだコマは重なりと判定しない", () => {
    expect(overlapsWithGutter([0, 0, 0.4, 0.4], [0.41, 0, 0.4, 0.4])).toBe(false);
    expect(overlapsWithGutter([0, 0, 0.4, 0.4], [0.39, 0, 0.4, 0.4])).toBe(true);
  });

  for (const fitMode of ["cover", "contain"] as const) {
    for (const anchor of ["center", "top", "bottom", "left", "right"] as const) {
      for (const focal of [null, [0.8, 0.2] as [number, number]]) {
        it(`${fitMode}/${anchor}/focal=${focal !== null}をレンダラー規則で配置する`, () => {
          const placement = computeImagePlacement(1600, 900, 600, 800, {
            fitMode,
            anchor,
            scale: 1.25,
            offsetX: 0.2,
            offsetY: -0.2,
            focal
          });
          expect(placement).not.toBeNull();
          if (fitMode === "contain") {
            expect(placement?.x).toBeGreaterThanOrEqual(0);
            expect(placement?.y).toBeGreaterThanOrEqual(0);
          } else {
            expect(placement?.x).toBeLessThanOrEqual(0);
            expect(placement?.y).toBeLessThanOrEqual(0);
            expect(placement?.width).toBeGreaterThanOrEqual(600);
            expect(placement?.height).toBeGreaterThanOrEqual(800);
          }
        });
      }
    }
  }

  it("coverのleft/right anchorをバックエンドと同じ切り出し位置へ変換する", () => {
    const left = computeImagePlacement(1600, 900, 600, 800, { fitMode: "cover", anchor: "left" });
    const right = computeImagePlacement(1600, 900, 600, 800, { fitMode: "cover", anchor: "right" });
    expect(left).toEqual({ x: 0, y: 0, width: 1422, height: 800 });
    expect(right).toEqual({ x: -822, y: 0, width: 1422, height: 800 });
  });

  it("containではanchorとfocalに依存せず中央配置する", () => {
    const placement = computeImagePlacement(1600, 900, 600, 800, {
      fitMode: "contain",
      anchor: "bottom",
      focal: [0.9, 0.1]
    });
    expect(placement).toEqual({ x: 0, y: 231, width: 600, height: 337 });
  });
});
