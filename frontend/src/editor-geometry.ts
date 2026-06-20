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
