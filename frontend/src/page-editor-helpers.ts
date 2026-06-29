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
