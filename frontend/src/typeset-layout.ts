// バックエンドの写植エンジン（backend/app/typeset.py）の「収まり計算」を忠実に移植したもの。
// 描画グリフのメトリクスには依存せず、1セル=size*1.06 の固定グリッドで行/列数・収まりを決める
// ため、Python版とビット単位で一致する結果が得られる。プレビュー（PageEditor）の文字サイズ判定を
// 最終レンダラーと揃え、「編集画面では収まって見えるのに出力で溢れる」乖離を防ぐ（領域7）。
//
// tests/fixtures/typeset_cases.json を共通フィクスチャとして、Python側テストとTS側テストの双方が
// このロジックの出力（font_size / line_count / fits）を突き合わせ、将来の片側だけの仕様変更を検出する。

// 行頭禁則（行・列の先頭に置けない文字）。typeset.LINE_START_FORBIDDENと一致させる。
const LINE_START_FORBIDDEN = new Set(
  "」』）］｝〕〉》】、。，．・？！ゝ々ー〜～：；,.!?）)]｠､｡" +
    "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ"
);
// 行末禁則（行・列の末尾に置けない文字）。
const LINE_END_FORBIDDEN = new Set("「『（［｛〔〈《【（([｟");
// 縦書きで90度回転させる文字（セル数には影響しないがトークン種別として保持する）。
const ROTATE_CHARS = new Set("ー－—‐-~〜～⁓―‒–゠（）()「」『』【】〈〉《》〔〕［］｛｝<>＜＞≪≫…‥");

// typographyのバックエンド既定（schemas.TypographySettings）。プレビューが既定値を知らない場合の退避。
export const DEFAULT_FONT_SIZE = 34;
export const DEFAULT_MIN_FONT_SIZE = 26;

// 吹き出し内側の余白（renderer.BUBBLE_INNER_PADと一致）。
export const BUBBLE_INNER_PAD = 12;
// 吹き出し形状ごとの内接係数（テキスト矩形を形状内へ収めるための拡大率）。
// renderer.SHAPE_INSCRIBE と一致させる。未知の形状は(1.05,1.05)。
export const SHAPE_INSCRIBE: Record<string, [number, number]> = {
  oval: [1.45, 1.45],
  burst: [1.95, 1.95],
  cloud: [1.3, 1.55],
  caption: [1.04, 1.04],
  none: [1.02, 1.02]
};

export type Token = { kind: "cjk" | "rot" | "tcy" | "break"; text: string };

export type GridLayout = {
  fontSize: number;
  vertical: boolean;
  lines: Token[][];
  lineCount: number;
  maxCells: number;
  cell: number;
  advance: number;
  fits: boolean;
  width: number;
  height: number;
};

function isTcyChar(ch: string): boolean {
  const code = ch.codePointAt(0) ?? 0;
  if (code > 127) return false;
  return /[0-9A-Za-z]/.test(ch) || "%#&+=".includes(ch);
}

export function tokenizeVertical(text: string): Token[] {
  const tokens: Token[] = [];
  const chars = [...text];
  let index = 0;
  while (index < chars.length) {
    const ch = chars[index];
    if (ch === "\n") {
      tokens.push({ kind: "break", text: "" });
      index += 1;
      continue;
    }
    if (isTcyChar(ch)) {
      let end = index;
      while (end < chars.length && isTcyChar(chars[end])) end += 1;
      const run = chars.slice(index, end);
      let pos = 0;
      while (pos < run.length) {
        // 英数字は2文字ずつ縦中横で1セルにまとめる（typeset.tokenize_verticalと同じ）。
        tokens.push({ kind: "tcy", text: run.slice(pos, pos + 2).join("") });
        pos += Math.min(2, run.length - pos);
      }
      index = end;
      continue;
    }
    tokens.push({ kind: ROTATE_CHARS.has(ch) ? "rot" : "cjk", text: ch });
    index += 1;
  }
  return tokens;
}

export function tokenizeHorizontal(text: string): Token[] {
  return [...text].map((ch) =>
    ch === "\n" ? ({ kind: "break", text: "" } as Token) : ({ kind: "cjk", text: ch } as Token)
  );
}

function applyKinsoku(lines: Token[][]): Token[][] {
  const result = lines.map((line) => [...line]);
  for (let i = 0; i < result.length - 1; i += 1) {
    const line = result[i];
    // 行末禁則の追い出し（開き括弧などを次行へ送る）。
    while (line.length && LINE_END_FORBIDDEN.has(line[line.length - 1].text.slice(0, 1))) {
      const moved = line.pop() as Token;
      result[i + 1].unshift(moved);
    }
  }
  return result.filter((line) => line.length > 0);
}

export function wrapTokens(tokens: Token[], cellsPerLine: number): Token[][] {
  const limit = Math.max(1, cellsPerLine);
  const lines: Token[][] = [];
  let current: Token[] = [];
  for (const token of tokens) {
    if (token.kind === "break") {
      lines.push(current);
      current = [];
      continue;
    }
    if (current.length >= limit) {
      const head = token.text.slice(0, 1);
      // 行頭禁則文字は現在行へ追い込む（最大+1セルまで）。
      if (LINE_START_FORBIDDEN.has(head) && current.length <= limit) {
        current.push(token);
        continue;
      }
      lines.push(current);
      current = [token];
    } else {
      current.push(token);
    }
  }
  if (current.length) lines.push(current);
  return applyKinsoku(lines);
}

function layoutAtSize(
  text: string,
  areaW: number,
  areaH: number,
  vertical: boolean,
  size: number,
  maxLines: number | null
): GridLayout {
  const cell = size * 1.06;
  const advance = size * 1.12;
  const tokens = vertical ? tokenizeVertical(text) : tokenizeHorizontal(text);
  const cellsPerLine = Math.max(1, Math.floor((vertical ? areaH : areaW) / cell));
  const lines = wrapTokens(tokens, cellsPerLine);
  const lineCount = Math.max(1, lines.length);
  const maxCells = lines.reduce((acc, line) => Math.max(acc, line.length), 1);
  const width = vertical ? lineCount * advance : maxCells * cell;
  const height = vertical ? maxCells * cell : lineCount * advance;
  const fits = width <= areaW + 0.5 && height <= areaH + 0.5 && (maxLines === null || lineCount <= maxLines);
  return { fontSize: size, vertical, lines, lineCount, maxCells, cell, advance, fits, width, height };
}

/**
 * 領域(areaW×areaH)へ収まる最大フォントサイズの行/列構成を返す。
 * backend/app/typeset.layout_text と同じ規則（default→minの降順、収まらなければmin）。
 */
export function layoutTextGrid(
  text: string,
  areaW: number,
  areaH: number,
  vertical: boolean,
  defaultSize: number,
  minSize: number,
  maxLines: number | null = null
): GridLayout {
  const top = Math.max(defaultSize, minSize);
  let chosen: GridLayout | null = null;
  for (let size = top; size >= minSize; size -= 1) {
    const layout = layoutAtSize(text, areaW, areaH, vertical, size, maxLines);
    chosen = layout;
    if (layout.fits) return layout;
  }
  return chosen ?? layoutAtSize(text, areaW, areaH, vertical, minSize, maxLines);
}
