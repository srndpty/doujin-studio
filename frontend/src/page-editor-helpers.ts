import { normalizeBox } from "./editor-geometry";
import type { Box } from "./editor-geometry";

// ページ実寸（rendererと一致）。
export const PAGE_W = 1200;
export const PAGE_H = 1700;
export const DISPLAY_W = 460;
export const SCALE = DISPLAY_W / PAGE_W;
export const SNAP = 0.01; // 1%グリッドへスナップ

export const LAYOUT_FAMILIES = [
  "establish",
  "dialogue",
  "reveal",
  "action",
  "punchline",
  "silent",
  "montage"
];
export const PANEL_ROLES = [
  "establish",
  "dialogue",
  "reaction",
  "action",
  "reveal",
  "emotional_peak",
  "silent",
  "transition",
  "punchline",
  "aftermath",
  "montage"
];
export const BACKGROUND_DENSITIES = ["", "none", "white", "light", "full"];
export const BALLOON_KINDS = ["oval", "cloud", "burst", "caption", "none"];
// 擬音のstyleプリセット（rendererのSFX_STYLE_PRESETSと一致させる）。
export const SFX_STYLES = ["small_handwritten", "handwritten", "impact", "quiet"];
// kind→既定の吹き出し形状（バックエンドのKIND_DEFAULT_BALLOONと一致させる）。
export const KIND_DEFAULT_BALLOON: Record<string, string> = {
  speech: "oval",
  monologue: "caption",
  narration: "caption",
  shout: "burst"
};

export const snap = (value: number) => Math.round(value / SNAP) * SNAP;
export const clamp01 = (value: number) => Math.max(0, Math.min(1, value));
export const clampFrame = (value: number) => Math.max(-0.05, Math.min(1.05, value));

export function rectFramePoints(bbox: Box): [number, number][] {
  const [x, y, w, h] = bbox;
  return [
    [x, y],
    [x + w, y],
    [x + w, y + h],
    [x, y + h]
  ];
}

export function panelFramePoints(panel: {
  bbox: Box;
  frame_points?: [number, number][] | null;
  shape_points?: [number, number][] | null;
}): [number, number][] {
  if (panel.frame_points?.length) return panel.frame_points;
  const [x, y, w, h] = panel.bbox;
  if (panel.shape_points?.length) {
    return panel.shape_points.map(([sx, sy]) => [x + sx * w, y + sy * h]);
  }
  return rectFramePoints(panel.bbox);
}

export function bboxFromFramePoints(points: [number, number][]): Box {
  const xs = points.map(([x]) => clamp01(x));
  const ys = points.map(([, y]) => clamp01(y));
  const x0 = Math.min(...xs);
  const y0 = Math.min(...ys);
  const x1 = Math.max(...xs);
  const y1 = Math.max(...ys);
  return normalizeBox([x0, y0, Math.max(0.01, x1 - x0), Math.max(0.01, y1 - y0)]);
}

function polygonArea(points: [number, number][]): number {
  let total = 0;
  for (let i = 0; i < points.length; i += 1) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    total += x1 * y2 - x2 * y1;
  }
  return Math.abs(total) / 2;
}

function orient(a: [number, number], b: [number, number], c: [number, number]): number {
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

function onSegment(a: [number, number], b: [number, number], c: [number, number]): boolean {
  return (
    Math.min(a[0], b[0]) <= c[0] &&
    c[0] <= Math.max(a[0], b[0]) &&
    Math.min(a[1], b[1]) <= c[1] &&
    c[1] <= Math.max(a[1], b[1])
  );
}

function segmentsIntersect(
  p1: [number, number],
  p2: [number, number],
  p3: [number, number],
  p4: [number, number]
): boolean {
  const d1 = orient(p3, p4, p1);
  const d2 = orient(p3, p4, p2);
  const d3 = orient(p1, p2, p3);
  const d4 = orient(p1, p2, p4);
  if (d1 > 0 !== d2 > 0 && d3 > 0 !== d4 > 0) return true;
  if (d1 === 0 && onSegment(p3, p4, p1)) return true;
  if (d2 === 0 && onSegment(p3, p4, p2)) return true;
  if (d3 === 0 && onSegment(p1, p2, p3)) return true;
  if (d4 === 0 && onSegment(p1, p2, p4)) return true;
  return false;
}

function hasSelfIntersection(points: [number, number][]): boolean {
  const n = points.length;
  const edges = points.map((point, i) => [point, points[(i + 1) % n]] as const);
  for (let i = 0; i < n; i += 1) {
    for (let j = i + 1; j < n; j += 1) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue;
      if (segmentsIntersect(edges[i][0], edges[i][1], edges[j][0], edges[j][1])) return true;
    }
  }
  return false;
}

// バックエンドのvalidate_frame_points（schemas.py）と同じ規則。保存前にフロントでも同じ
// エラーを表示し、422で弾かれる前に気づけるようにする（領域4）。
export function framePointsError(points: [number, number][]): string | null {
  if (points.length < 3 || points.length > 12) return "頂点は3〜12点にしてください";
  if (points.some(([x, y]) => !Number.isFinite(x) || !Number.isFinite(y)))
    return "頂点は有限の数値で指定してください";
  if (points.some(([x, y]) => x < -0.05 || x > 1.05 || y < -0.05 || y > 1.05))
    return "頂点はページ座標の-0.05〜1.05に収めてください";
  const n = points.length;
  if (points.some((p, i) => p[0] === points[(i + 1) % n][0] && p[1] === points[(i + 1) % n][1]))
    return "連続する重複頂点があります";
  if (polygonArea(points) < 1e-6) return "面積がゼロに近い形状です";
  if (hasSelfIntersection(points)) return "辺が自己交差しています";
  return null;
}

export function transformFramePoints(points: [number, number][], from: Box, to: Box): [number, number][] {
  const [fromX, fromY, fromW, fromH] = from;
  const [toX, toY, toW, toH] = to;
  const sx = toW / Math.max(fromW, 0.0001);
  const sy = toH / Math.max(fromH, 0.0001);
  return points.map(([x, y]) => [
    snap(clampFrame(toX + (x - fromX) * sx)),
    snap(clampFrame(toY + (y - fromY) * sy))
  ]);
}
