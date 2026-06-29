export function parseNodeIdList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export const ANIMA3_POSITIVE = "masterpiece, best quality, score_7, safe, anime";
export const ANIMA3_NEGATIVE =
  "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia, bad hands, bad anatomy, extra fingers, missing fingers, text, watermark, speech bubble, logo";
