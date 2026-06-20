export type Box = [number, number, number, number];

const SNAP = 0.01;
const snap = (value: number) => Math.round(value / SNAP) * SNAP;

export function normalizeBox(box: Box, minimum = 0.04): Box {
  const width = Math.max(minimum, Math.min(1, snap(box[2])));
  const height = Math.max(minimum, Math.min(1, snap(box[3])));
  const x = Math.max(0, Math.min(1 - width, snap(box[0])));
  const y = Math.max(0, Math.min(1 - height, snap(box[1])));
  return [x, y, width, height];
}

export function overlapsWithGutter(a: Box, b: Box, gutter = 0.004): boolean {
  return !(
    a[0] + a[2] + gutter <= b[0] ||
    b[0] + b[2] + gutter <= a[0] ||
    a[1] + a[3] + gutter <= b[1] ||
    b[1] + b[3] + gutter <= a[1]
  );
}

export type CropSettings = {
  fitMode: "cover" | "contain";
  anchor: "center" | "top" | "bottom" | "left" | "right";
  scale?: number;
  offsetX?: number;
  offsetY?: number;
  focal?: [number, number] | null;
};

export type ImagePlacement = { x: number; y: number; width: number; height: number };
const clamp01 = (value: number) => Math.max(0, Math.min(1, value));

export function computeImagePlacement(
  sourceWidth: number,
  sourceHeight: number,
  targetWidth: number,
  targetHeight: number,
  settings: CropSettings
): ImagePlacement | null {
  if (sourceWidth <= 0 || sourceHeight <= 0 || targetWidth <= 0 || targetHeight <= 0) return null;
  if (settings.fitMode === "contain") {
    const ratio = Math.min(targetWidth / sourceWidth, targetHeight / sourceHeight);
    const width = Math.max(1, Math.trunc(sourceWidth * ratio));
    const height = Math.max(1, Math.trunc(sourceHeight * ratio));
    return {
      x: Math.floor((targetWidth - width) / 2),
      y: Math.floor((targetHeight - height) / 2),
      width,
      height
    };
  }

  const ratio =
    Math.max(targetWidth / sourceWidth, targetHeight / sourceHeight) * Math.max(1, settings.scale ?? 1);
  const width = Math.max(1, Math.trunc(sourceWidth * ratio));
  const height = Math.max(1, Math.trunc(sourceHeight * ratio));
  const extraX = Math.max(0, width - targetWidth);
  const extraY = Math.max(0, height - targetHeight);
  let left: number;
  let top: number;
  if (settings.focal) {
    left = Math.trunc(settings.focal[0] * width - targetWidth / 2);
    top = Math.trunc(settings.focal[1] * height - targetHeight / 2);
  } else {
    const anchorX = settings.anchor === "left" ? 0 : settings.anchor === "right" ? 1 : 0.5;
    const anchorY = settings.anchor === "top" ? 0 : settings.anchor === "bottom" ? 1 : 0.5;
    left = Math.trunc(extraX * clamp01(anchorX + (settings.offsetX ?? 0) * 0.5));
    top = Math.trunc(extraY * clamp01(anchorY + (settings.offsetY ?? 0) * 0.5));
  }
  left = Math.max(0, Math.min(left, extraX));
  top = Math.max(0, Math.min(top, extraY));
  return { x: left === 0 ? 0 : -left, y: top === 0 ? 0 : -top, width, height };
}
