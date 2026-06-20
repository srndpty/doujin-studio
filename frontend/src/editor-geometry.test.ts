import { describe, expect, it } from "vitest";
import { normalizeBox, overlapsWithGutter } from "./editor-geometry";

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
});
