import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  BUBBLE_INNER_PAD,
  SHAPE_INSCRIBE,
  cellPlacementCenter,
  layoutTextGrid,
  verticalCellPlacement
} from "./typeset-layout";

// バックエンドの typeset.layout_text と同じ収まり判定を共通フィクスチャで突き合わせる（領域7）。
// 同じ tests/fixtures/typeset_cases.json を tests/test_typeset_fixtures.py も検証しており、
// プレビュー（TS）と最終レンダラー（Python）のどちらかだけが変わると検出できる。
type TypesetCase = {
  name: string;
  text: string;
  vertical: boolean;
  balloon: string;
  bubble_w: number;
  bubble_h: number;
  default_size: number;
  min_size: number;
  max_lines: number;
  expect: {
    font_size: number;
    line_count: number;
    fits: boolean;
    lines: { kind: string; text: string }[][];
  };
};

// フィクスチャはリポジトリ共通(tests/fixtures)に置く。vitestのcwdはfrontend想定だが、
// リポジトリルートから実行された場合にも追従できるよう両候補を探す。
const fixtureCandidates = [
  resolve(process.cwd(), "../tests/fixtures/typeset_cases.json"),
  resolve(process.cwd(), "tests/fixtures/typeset_cases.json")
];
const fixturePath = fixtureCandidates.find((candidate) => existsSync(candidate));
if (!fixturePath) throw new Error("typeset_cases.json が見つかりません");
const cases = JSON.parse(readFileSync(fixturePath, "utf-8")) as TypesetCase[];

describe("写植グリッド（最終レンダラーとの共通フィクスチャ）", () => {
  it.each(cases.map((c) => [c.name, c] as const))("%s", (_name, testCase) => {
    const [fx, fy] = SHAPE_INSCRIBE[testCase.balloon] ?? [1.05, 1.05];
    const innerW = Math.max(8, (testCase.bubble_w - BUBBLE_INNER_PAD * 2) / fx);
    const innerH = Math.max(8, (testCase.bubble_h - BUBBLE_INNER_PAD * 2) / fy);
    const grid = layoutTextGrid(
      testCase.text,
      innerW,
      innerH,
      testCase.vertical,
      testCase.default_size,
      testCase.min_size,
      testCase.max_lines
    );
    expect(grid.fontSize).toBe(testCase.expect.font_size);
    expect(grid.lineCount).toBe(testCase.expect.line_count);
    expect(grid.fits).toBe(testCase.expect.fits);
    // 行/列のtoken分割（禁則・手動改行・縦中横）までPythonと完全一致を要求する（領域5）。
    const actualLines = grid.lines.map((line) =>
      line.map((token) => ({ kind: token.kind, text: token.text }))
    );
    expect(actualLines).toEqual(testCase.expect.lines);
  });
});

describe("縦書きセル配置（回転トークンの中心揃え・領域2）", () => {
  it("回転トークンと通常トークンは同じセル中心に置かれる", () => {
    const cell = 36;
    const colCx = 200;
    const cellCy = 150;
    const normal = verticalCellPlacement(false, colCx, cellCy, cell);
    const rotated = verticalCellPlacement(true, colCx, cellCy, cell);
    // 視覚中心はどちらも (colCx, cellCy) で一致する（半セルずれがない）。
    expect(cellPlacementCenter(normal, cell)).toEqual([colCx, cellCy]);
    expect(cellPlacementCenter(rotated, cell)).toEqual([colCx, cellCy]);
    // 回転トークンは中心を軸(offset=cell/2)に-90度回し、x/yへ中心座標を渡す。
    expect(rotated).toEqual({ x: colCx, y: cellCy, offsetX: cell / 2, offsetY: cell / 2, rotation: -90 });
    // 通常トークンは左上基準・無回転。
    expect(normal).toEqual({
      x: colCx - cell / 2,
      y: cellCy - cell / 2,
      offsetX: 0,
      offsetY: 0,
      rotation: 0
    });
  });
});
