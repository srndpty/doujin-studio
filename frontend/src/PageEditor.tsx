import { useEffect, useMemo, useRef, useState } from "react";
import {
  Ellipse,
  Group,
  Image as KonvaImage,
  Layer,
  Line,
  Rect,
  Stage,
  Text,
  Transformer
} from "react-konva";
import type Konva from "konva";
import { api } from "./api/client";
import type { Dialogue, MangaPage, MangaProject, OverlayElement, Panel, Sfx } from "./App";
import { computeImagePlacement, normalizeBox, overlapsWithGutter } from "./editor-geometry";
import type { Box } from "./editor-geometry";

// ページ実寸（rendererと一致）。
const PAGE_W = 1200;
const PAGE_H = 1700;
const DISPLAY_W = 460;
const SCALE = DISPLAY_W / PAGE_W;
const SNAP = 0.01; // 1%グリッドへスナップ

const LAYOUT_FAMILIES = ["establish", "dialogue", "reveal", "action", "punchline", "silent", "montage"];
const PANEL_ROLES = [
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
const BACKGROUND_DENSITIES = ["", "none", "white", "light", "full"];
const BALLOON_KINDS = ["oval", "cloud", "burst", "caption", "none"];
// 擬音のstyleプリセット（rendererのSFX_STYLE_PRESETSと一致させる）。
const SFX_STYLES = ["small_handwritten", "handwritten", "impact", "quiet"];
// kind→既定の吹き出し形状（バックエンドのKIND_DEFAULT_BALLOONと一致させる）。
const KIND_DEFAULT_BALLOON: Record<string, string> = {
  speech: "oval",
  monologue: "caption",
  narration: "caption",
  shout: "burst"
};

function shapePointsForPreset(preset: string): [number, number][] | null {
  if (preset === "slant-right")
    return [
      [0.12, 0],
      [1, 0],
      [0.88, 1],
      [0, 1]
    ];
  if (preset === "slant-left")
    return [
      [0, 0],
      [0.88, 0],
      [1, 1],
      [0.12, 1]
    ];
  if (preset === "trapezoid")
    return [
      [0.12, 0],
      [0.88, 0],
      [1, 1],
      [0, 1]
    ];
  return null;
}

function shapePreset(points: [number, number][] | null | undefined): string {
  if (!points) return "rectangle";
  if (points[0]?.[0] === 0.12 && points[1]?.[0] === 1) return "slant-right";
  if (points[0]?.[0] === 0 && points[1]?.[0] === 0.88) return "slant-left";
  return "trapezoid";
}

type Point = [number, number];
type PreflightIssue = {
  level: "error" | "warning";
  code: string;
  message: string;
  page: number;
  panel_id?: string | null;
  category?: string;
  suggestion?: string;
  fixable?: boolean;
};

type Props = {
  projectId: string;
  // 楽観ロック用。manga-json保存時に?revision=で送る。
  revision: number;
  manga: MangaProject;
  pageNumber: number;
  assetVersion: number;
  busy: boolean;
  // revisionを伴う場合(サーバ保存後)は親が楽観ロック用に同期する。
  onChange: (manga: MangaProject, revision?: number) => void;
  onSave: (manga: MangaProject) => Promise<void> | void;
  onSuggest: (family: string | null) => Promise<void> | void;
  onOverlayUpload: (
    manga: MangaProject,
    pageNumber: number,
    overlayId: string,
    kind: "asset" | "mask",
    file: File
  ) => Promise<boolean> | boolean;
  setMessage: (text: string) => void;
  // アセットのキャッシュバスターを進める（同一URLの画像差し替えを反映する）。
};

function useAssetImage(asset: string | null, version: number): HTMLImageElement | null {
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    if (!asset) {
      setImg(null);
      return;
    }
    const image = new window.Image();
    image.src = assetUrl(asset, version);
    image.onload = () => setImg(image);
    return () => {
      image.onload = null;
    };
  }, [asset, version]);
  return img;
}

function assetUrl(asset: string, version: number): string {
  const normalized = asset.replaceAll("\\", "/").replace(/^exports\//, "");
  return `/api/assets/${normalized.split("/").map(encodeURIComponent).join("/")}?v=${version}`;
}

// mask_assetがあれば輝度をアルファとして切り抜き、renderer.draw_overlayと同じ見た目をプレビューする。
function useMaskedImage(
  asset: string | null,
  mask: string | null | undefined,
  version: number
): HTMLImageElement | null {
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    if (!asset) {
      setImg(null);
      return;
    }
    let cancelled = false;
    const base = new window.Image();
    base.onload = () => {
      if (cancelled) return;
      if (!mask) {
        setImg(base);
        return;
      }
      const maskImage = new window.Image();
      maskImage.onload = () => {
        if (cancelled) return;
        try {
          const canvas = document.createElement("canvas");
          canvas.width = base.naturalWidth;
          canvas.height = base.naturalHeight;
          const ctx = canvas.getContext("2d");
          if (!ctx) {
            setImg(base);
            return;
          }
          ctx.drawImage(base, 0, 0);
          const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
          const maskCanvas = document.createElement("canvas");
          maskCanvas.width = canvas.width;
          maskCanvas.height = canvas.height;
          const maskCtx = maskCanvas.getContext("2d");
          if (!maskCtx) {
            setImg(base);
            return;
          }
          maskCtx.drawImage(maskImage, 0, 0, canvas.width, canvas.height);
          const maskPixels = maskCtx.getImageData(0, 0, canvas.width, canvas.height);
          for (let i = 0; i < pixels.data.length; i += 4) {
            pixels.data[i + 3] =
              0.299 * maskPixels.data[i] + 0.587 * maskPixels.data[i + 1] + 0.114 * maskPixels.data[i + 2];
          }
          ctx.putImageData(pixels, 0, 0);
          const composed = new window.Image();
          composed.onload = () => {
            if (!cancelled) setImg(composed);
          };
          composed.src = canvas.toDataURL();
        } catch {
          setImg(base); // canvasが使えない環境ではマスクなしで表示する。
        }
      };
      maskImage.src = assetUrl(mask, version);
    };
    base.src = assetUrl(asset, version);
    return () => {
      cancelled = true;
    };
  }, [asset, mask, version]);
  return img;
}

const snap = (value: number) => Math.round(value / SNAP) * SNAP;
const clamp01 = (value: number) => Math.max(0, Math.min(1, value));

export function PageEditor({
  projectId,
  manga: mangaProp,
  pageNumber,
  assetVersion,
  busy,
  onChange,
  onSave,
  onSuggest,
  onOverlayUpload,
  setMessage
}: Props) {
  // 親のonChangeが反映される前でも編集を即時表示できるよう、作業コピーを持つ。
  const [manga, setManga] = useState<MangaProject>(mangaProp);
  useEffect(() => {
    setManga(mangaProp);
  }, [mangaProp]);
  const page = manga.pages.find((item) => item.page === pageNumber) ?? null;
  const [selection, setSelection] = useState<
    | { panelId: string; kind: "panel" | "dialogue" | "sfx"; index: number }
    | { overlayId: string; kind: "overlay" }
    | null
  >(null);
  const [family, setFamily] = useState<string>("");
  const [preflight, setPreflight] = useState<{
    ok: boolean;
    errors: PreflightIssue[];
    warnings: PreflightIssue[];
  } | null>(null);
  const [reviewCategory, setReviewCategory] = useState<string>("all");
  const [showRegions, setShowRegions] = useState(false);

  const runPreflight = async () => {
    try {
      // 現在の編集内容を本文で渡し、非破壊で検査する（保存しないのでレンダリング状態を消さない）。
      const result = await api.post<{
        ok: boolean;
        errors: PreflightIssue[];
        warnings: PreflightIssue[];
      }>(`/api/projects/${projectId}/pages/${pageNumber}/preflight`, manga);
      setPreflight(result);
      setMessage(
        result.ok ? "プリフライト: 重大エラーなし" : `プリフライト: ${result.errors.length}件のエラー`
      );
    } catch (error) {
      setMessage(`プリフライトに失敗しました: ${(error as Error).message}`);
    }
  };
  const rectRefs = useRef<Record<string, Konva.Rect>>({});
  const transformerRef = useRef<Konva.Transformer | null>(null);

  useEffect(() => {
    setSelection(null);
  }, [pageNumber, projectId]);

  // パネル選択時にTransformerを取り付ける。
  useEffect(() => {
    const transformer = transformerRef.current;
    if (!transformer) return;
    if (selection?.kind === "panel" && rectRefs.current[selection.panelId]) {
      transformer.nodes([rectRefs.current[selection.panelId]]);
    } else {
      transformer.nodes([]);
    }
    transformer.getLayer()?.batchDraw();
  }, [selection, page]);

  const mutatePage = (mutator: (page: MangaPage) => void): MangaProject => {
    const next = structuredClone(manga);
    const target = next.pages.find((item) => item.page === pageNumber);
    if (!target) return next;
    mutator(target);
    setManga(next);
    onChange(next);
    return next;
  };

  const updatePanelBbox = (panelId: string, bbox: [number, number, number, number]) => {
    const normalized = normalizeBox(bbox);
    const overlaps = page?.panels.some((panel) => {
      if (panel.panel_id === panelId) return false;
      return overlapsWithGutter(normalized, panel.bbox);
    });
    if (overlaps) {
      setMessage("コマの重なりまたはガター不足を防ぐため変更を取り消しました");
      return;
    }
    mutatePage((target) => {
      const panel = target.panels.find((item) => item.panel_id === panelId);
      if (panel) panel.bbox = normalized;
    });
  };

  const renumberReadingOrder = () => {
    if (!page) return;
    const rtl = manga.reading_direction !== "ltr";
    const ordered = [...page.panels]
      .map((panel, index) => ({ panel, index }))
      .sort((a, b) => {
        const [ax, ay, , ah] = a.panel.bbox;
        const [bx, by, , bh] = b.panel.bbox;
        const sameRow = Math.abs(ay - by) < Math.min(ah, bh) * 0.5;
        if (sameRow) return rtl ? bx - ax : ax - bx;
        return ay - by;
      })
      .map((entry) => entry.panel.panel_id);
    mutatePage((target) => {
      target.reading_order = ordered;
    });
    setMessage("読み順を位置から振り直しました");
  };

  const readingIndex = (panelId: string): number => {
    const order: string[] = page?.reading_order ?? [];
    const found = order.indexOf(panelId);
    return found >= 0 ? found + 1 : (page?.panels.findIndex((panel) => panel.panel_id === panelId) ?? -1) + 1;
  };

  const selectedPanel = useMemo(
    () =>
      selection && selection.kind !== "overlay"
        ? (page?.panels.find((panel) => panel.panel_id === selection.panelId) ?? null)
        : null,
    [selection, page]
  );
  const selectedOverlay = useMemo(
    () =>
      selection?.kind === "overlay"
        ? ((page?.overlay_elements ?? []).find((overlay) => overlay.id === selection.overlayId) ?? null)
        : null,
    [selection, page]
  );

  const patchOverlay = (overlayId: string, patch: Partial<OverlayElement>) => {
    mutatePage((target) => {
      const overlay = (target.overlay_elements ?? []).find((item) => item.id === overlayId);
      if (overlay) Object.assign(overlay, patch);
    });
  };

  const addOverlay = () => {
    const overlayId = `overlay_${Date.now()}`;
    mutatePage((target) => {
      target.overlay_elements ??= [];
      target.overlay_elements.push({
        id: overlayId,
        source_panel_id: target.panels[0]?.panel_id ?? "",
        asset: null,
        mask_asset: null,
        box: [0.25, 0.25, 0.5, 0.5],
        scale: 1,
        opacity: 1,
        layer: "front",
        z_index: 0,
        occluded_by_panel_ids: []
      });
    });
    setSelection({ kind: "overlay", overlayId });
  };

  const uploadOverlay = async (overlay: OverlayElement, kind: "asset" | "mask", file: File) => {
    const completed = await onOverlayUpload(manga, pageNumber, overlay.id, kind, file);
    if (!completed) return;
    setMessage(kind === "asset" ? "overlay画像を登録しました" : "overlayマスクを登録しました");
  };

  if (!page) return <p className="hint">ページがありません。先にネームを生成してください。</p>;

  const renderOverlay = (overlay: OverlayElement) => (
    <OverlayNode
      key={overlay.id}
      overlay={overlay}
      version={assetVersion}
      selected={selection?.kind === "overlay" && selection.overlayId === overlay.id}
      onSelect={() => setSelection({ kind: "overlay", overlayId: overlay.id })}
      onMove={(box) => patchOverlay(overlay.id, { box })}
    />
  );
  // サーバーの描画順に合わせ、背面overlayはコマより先、前面overlayはコマより後に描く。
  const sortedOverlays = [...(page.overlay_elements ?? [])].sort((a, b) => a.z_index - b.z_index);
  const backOverlays = sortedOverlays.filter((overlay) => overlay.layer === "back");
  const frontOverlays = sortedOverlays.filter((overlay) => overlay.layer === "front");
  const reviewIssues = [...(preflight?.errors ?? []), ...(preflight?.warnings ?? [])];
  const reviewCategories = Array.from(new Set(reviewIssues.map((issue) => issue.category ?? "general")));
  const activeReviewCategory =
    reviewCategory === "all" || reviewCategories.includes(reviewCategory) ? reviewCategory : "all";
  const visibleReviewIssues =
    activeReviewCategory === "all"
      ? reviewIssues
      : reviewIssues.filter((issue) => (issue.category ?? "general") === activeReviewCategory);

  return (
    <div className="page-editor">
      <div className="page-editor-canvas">
        <Stage
          width={PAGE_W * SCALE}
          height={PAGE_H * SCALE}
          style={{ background: "#f8f8f4", border: "1px solid #ccc" }}
          onMouseDown={(event) => {
            if (event.target === event.target.getStage()) setSelection(null);
          }}
        >
          <Layer scaleX={SCALE} scaleY={SCALE}>
            {backOverlays.map(renderOverlay)}
            {page.panels.map((panel) => (
              <PanelNode
                key={panel.panel_id}
                panel={panel}
                version={assetVersion}
                readingNumber={readingIndex(panel.panel_id)}
                selected={selection?.kind === "panel" && selection.panelId === panel.panel_id}
                registerRef={(node: Konva.Rect | null) => {
                  if (node) rectRefs.current[panel.panel_id] = node;
                }}
                onSelect={() => setSelection({ panelId: panel.panel_id, kind: "panel", index: 0 })}
                onBbox={(bbox: [number, number, number, number]) => updatePanelBbox(panel.panel_id, bbox)}
                onSelectDialogue={(index: number) =>
                  setSelection({ panelId: panel.panel_id, kind: "dialogue", index })
                }
                onSelectSfx={(index: number) => setSelection({ panelId: panel.panel_id, kind: "sfx", index })}
                onMoveDialogue={(index: number, box: [number, number, number, number]) =>
                  mutatePage((target) => {
                    const p = target.panels.find((item) => item.panel_id === panel.panel_id);
                    if (p && p.dialogue[index]) p.dialogue[index].box = box;
                  })
                }
                onMoveTail={(index: number, tip: [number, number]) =>
                  mutatePage((target) => {
                    const p = target.panels.find((item) => item.panel_id === panel.panel_id);
                    if (p && p.dialogue[index]) {
                      const tail = p.dialogue[index].tail ?? { enabled: true, base: 0.5, width: 0.16 };
                      p.dialogue[index].tail = { ...tail, tip };
                    }
                  })
                }
                onMoveSfx={(index: number, box: [number, number]) =>
                  mutatePage((target) => {
                    const p = target.panels.find((item) => item.panel_id === panel.panel_id);
                    if (p && p.sfx[index]) p.sfx[index].box = box;
                  })
                }
                showRegions={showRegions}
                onMoveCharacterRegion={(characterId, box) =>
                  mutatePage((target) => {
                    const p = target.panels.find((item) => item.panel_id === panel.panel_id);
                    const entry = (p?.character_layout ?? []).find((item) => item.id === characterId);
                    if (entry) entry.region_box = box;
                  })
                }
                showTail={selection?.kind === "dialogue" && selection.panelId === panel.panel_id}
                selectedDialogue={
                  selection?.kind === "dialogue" && selection.panelId === panel.panel_id
                    ? selection.index
                    : -1
                }
              />
            ))}
            {frontOverlays.map(renderOverlay)}
            <Transformer
              ref={transformerRef}
              rotateEnabled={false}
              keepRatio={false}
              boundBoxFunc={(_oldBox, newBox) => newBox}
            />
          </Layer>
        </Stage>
      </div>

      <div className="page-editor-controls">
        <div className="editor-row">
          <strong>ページ {page.page}</strong>
          <span className="hint">
            {page.layout_family || "未設定"} {page.layout_locked ? "🔒" : ""}
          </span>
        </div>

        <fieldset>
          <legend>レイアウト再提案</legend>
          <select value={family} onChange={(event) => setFamily(event.target.value)}>
            <option value="">自動（隣接ページと差別化）</option>
            {LAYOUT_FAMILIES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <button disabled={busy} onClick={() => onSuggest(family || null)}>
            このページを再レイアウト
          </button>
          <button disabled={busy} onClick={renumberReadingOrder}>
            読み順を振り直す
          </button>
        </fieldset>

        {selectedPanel && selection?.kind === "panel" && (
          <>
            <fieldset>
              <legend>コマ形状</legend>
              <select
                value={shapePreset(selectedPanel.shape_points)}
                onChange={(event) =>
                  mutatePage((target) => {
                    const panel = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                    if (panel) panel.shape_points = shapePointsForPreset(event.target.value);
                  })
                }
              >
                <option value="rectangle">長方形</option>
                <option value="slant-right">右傾斜</option>
                <option value="slant-left">左傾斜</option>
                <option value="trapezoid">台形</option>
              </select>
              <small className="hint">変形コマも外接bboxをドラッグして移動・拡縮できます。</small>
            </fieldset>
            <CropControls
              panel={selectedPanel}
              onChange={(generation) =>
                mutatePage((target) => {
                  const p = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                  if (p) p.generation = { ...p.generation, ...generation };
                })
              }
              onSubjectMode={(mode) =>
                mutatePage((target) => {
                  const p = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                  if (p) p.subject_mode = mode;
                })
              }
            />
            <DirectingControls
              page={page}
              panel={selectedPanel}
              showRegions={showRegions}
              onShowRegions={setShowRegions}
              onPageChange={(patch) =>
                mutatePage((target) => {
                  Object.assign(target, patch);
                })
              }
              onPanelChange={(patch) =>
                mutatePage((target) => {
                  const p = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                  if (p) Object.assign(p, patch);
                })
              }
              onCharacterRegion={(characterId, box) =>
                mutatePage((target) => {
                  const p = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                  const entry = (p?.character_layout ?? []).find((item) => item.id === characterId);
                  if (entry) entry.region_box = box;
                })
              }
            />
          </>
        )}

        {selection?.kind === "dialogue" && selectedPanel?.dialogue[selection.index] && (
          <BalloonControls
            dialogue={selectedPanel.dialogue[selection.index]}
            onChange={(patch) =>
              mutatePage((target) => {
                const p = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                if (p && p.dialogue[selection.index])
                  p.dialogue[selection.index] = { ...p.dialogue[selection.index], ...patch };
              })
            }
          />
        )}

        {selection?.kind === "sfx" && selectedPanel?.sfx[selection.index] && (
          <SfxControls
            sfx={selectedPanel.sfx[selection.index]}
            onChange={(patch) =>
              mutatePage((target) => {
                const p = target.panels.find((item) => item.panel_id === selectedPanel.panel_id);
                if (p && p.sfx[selection.index])
                  p.sfx[selection.index] = { ...p.sfx[selection.index], ...patch };
              })
            }
          />
        )}

        <fieldset>
          <legend>オーバーフレーム</legend>
          <button disabled={busy} onClick={addOverlay}>
            追加
          </button>
          {selectedOverlay && (
            <OverlayControls
              overlay={selectedOverlay}
              panels={page.panels}
              onChange={(patch) => patchOverlay(selectedOverlay.id, patch)}
              onUpload={(kind, file) => {
                void uploadOverlay(selectedOverlay, kind, file).catch((error: Error) =>
                  setMessage(`overlayのアップロードに失敗しました: ${error.message}`)
                );
              }}
              onDelete={() => {
                mutatePage((target) => {
                  target.overlay_elements = (target.overlay_elements ?? []).filter(
                    (item) => item.id !== selectedOverlay.id
                  );
                });
                setSelection(null);
              }}
            />
          )}
        </fieldset>

        <fieldset>
          <legend>漫画レビュー</legend>
          <button disabled={busy} onClick={runPreflight}>
            このページを検査
          </button>
          {reviewIssues.length > 0 && (
            <label>
              レビュー分類
              <select
                value={activeReviewCategory}
                onChange={(event) => setReviewCategory(event.target.value)}
              >
                <option value="all">すべて</option>
                {reviewCategories.map((category) => (
                  <option key={category} value={category}>
                    {category}
                  </option>
                ))}
              </select>
            </label>
          )}
          {preflight && (
            <ul className="preflight-list">
              {reviewIssues.length === 0 && <li className="ok">問題は見つかりませんでした</li>}
              {visibleReviewIssues.map((issue, index) => (
                <li key={`${issue.level}${index}`} className={issue.level}>
                  <button
                    type="button"
                    disabled={!issue.panel_id}
                    onClick={() => {
                      if (!issue.panel_id) return;
                      // SFXのissueは擬音編集欄を直接開く（文字を打ち直して修正できるようにする）。
                      const kind = (issue.category ?? "") === "sfx" ? "sfx" : "panel";
                      setSelection({ panelId: issue.panel_id, kind, index: 0 });
                    }}
                  >
                    {issue.level === "error" ? "エラー" : "警告"} / {issue.category ?? "general"}:{" "}
                    {issue.message}
                  </button>
                  {issue.suggestion && <small>{issue.suggestion}</small>}
                </li>
              ))}
            </ul>
          )}
        </fieldset>

        <div className="editor-row">
          <button
            className="primary"
            disabled={busy}
            onClick={() => {
              const next = mutatePage((target) => {
                target.layout_locked = true;
              });
              void onSave(next);
            }}
          >
            保存（レイアウト確定）
          </button>
        </div>
        <p className="hint">
          パネルをドラッグで移動・四隅で拡縮（1%グリッドにスナップ）。吹き出し・SFXもドラッグで移動できます。
        </p>
      </div>
    </div>
  );
}

type PanelNodeProps = {
  panel: Panel;
  version: number;
  readingNumber: number;
  selected: boolean;
  registerRef: (node: Konva.Rect | null) => void;
  onSelect: () => void;
  onBbox: (box: Box) => void;
  onSelectDialogue: (index: number) => void;
  onSelectSfx: (index: number) => void;
  onMoveDialogue: (index: number, box: Box) => void;
  onMoveTail: (index: number, tip: Point) => void;
  onMoveSfx: (index: number, box: Point) => void;
  showRegions: boolean;
  onMoveCharacterRegion: (characterId: string, box: Box) => void;
  showTail: boolean;
  selectedDialogue: number;
};

function PanelNode(props: PanelNodeProps) {
  const { panel, version, readingNumber, selected, registerRef, onSelect, onBbox } = props;
  const image = useAssetImage(panel.image_asset, version);
  const [px, py, pw, ph] = panel.bbox.map(
    (value: number, i: number) => value * (i % 2 === 0 ? PAGE_W : PAGE_H)
  );
  const polygonPoints = panel.shape_points?.flatMap(([x, y]) => [px + x * pw, py + y * ph]) ?? [];

  const handleTransform = (node: Konva.Rect) => {
    const scaleX = node.scaleX();
    const scaleY = node.scaleY();
    node.scaleX(1);
    node.scaleY(1);
    const bbox: [number, number, number, number] = [
      clamp01(snap(node.x() / PAGE_W)),
      clamp01(snap(node.y() / PAGE_H)),
      clamp01(snap((node.width() * scaleX) / PAGE_W)),
      clamp01(snap((node.height() * scaleY) / PAGE_H))
    ];
    onBbox(bbox);
  };

  const placement = useMemo(() => {
    if (!image) return null;
    return computeImagePlacement(image.width, image.height, pw, ph, {
      fitMode: panel.generation.fit_mode,
      anchor: panel.generation.crop_anchor,
      scale: panel.generation.crop_scale,
      offsetX: panel.generation.crop_offset_x,
      offsetY: panel.generation.crop_offset_y,
      focal:
        panel.generation.focal_x != null && panel.generation.focal_y != null
          ? [panel.generation.focal_x, panel.generation.focal_y]
          : null
    });
  }, [
    image,
    pw,
    ph,
    panel.generation.fit_mode,
    panel.generation.crop_anchor,
    panel.generation?.crop_scale,
    panel.generation?.crop_offset_x,
    panel.generation?.crop_offset_y,
    panel.generation.focal_x,
    panel.generation.focal_y
  ]);

  return (
    <Group>
      <Group
        clipX={panel.shape_points ? undefined : px}
        clipY={panel.shape_points ? undefined : py}
        clipWidth={panel.shape_points ? undefined : pw}
        clipHeight={panel.shape_points ? undefined : ph}
        clipFunc={
          panel.shape_points
            ? (context) => {
                context.beginPath();
                panel.shape_points?.forEach(([x, y], index) => {
                  if (index === 0) context.moveTo(px + x * pw, py + y * ph);
                  else context.lineTo(px + x * pw, py + y * ph);
                });
                context.closePath();
              }
            : undefined
        }
      >
        {panel.generation.fit_mode === "contain" && (
          <Rect x={px} y={py} width={pw} height={ph} fill="#f5f5f2" listening={false} />
        )}
        {image && placement && (
          <KonvaImage
            image={image}
            x={px + placement.x}
            y={py + placement.y}
            width={placement.width}
            height={placement.height}
          />
        )}
      </Group>
      <Rect
        ref={registerRef}
        x={px}
        y={py}
        width={pw}
        height={ph}
        stroke={panel.shape_points ? "transparent" : selected ? "#2b6cff" : "#141414"}
        strokeWidth={selected ? 6 : 4}
        draggable
        onClick={onSelect}
        onTap={onSelect}
        onDragEnd={(event) =>
          onBbox([
            clamp01(snap(event.target.x() / PAGE_W)),
            clamp01(snap(event.target.y() / PAGE_H)),
            panel.bbox[2],
            panel.bbox[3]
          ])
        }
        onTransformEnd={(event) => handleTransform(event.target as Konva.Rect)}
        fill="rgba(0,0,0,0.001)"
      />
      {panel.shape_points && (
        <Line
          points={polygonPoints}
          closed
          stroke={selected ? "#2b6cff" : "#141414"}
          strokeWidth={selected ? 6 : 4}
          listening={false}
        />
      )}
      <Group>
        <Rect
          x={px + 8}
          y={py + 8}
          width={48}
          height={36}
          fill="#000000aa"
          cornerRadius={6}
          listening={false}
        />
        <Text
          x={px + 8}
          y={py + 14}
          width={48}
          text={String(readingNumber)}
          align="center"
          fontSize={24}
          fill="#fff"
          listening={false}
        />
      </Group>
      {props.showRegions &&
        (panel.character_layout ?? []).map((entry) => {
          const box = entry.region_box ?? [0.18, 0.08, 0.64, 0.84];
          const rx = px + box[0] * pw;
          const ry = py + box[1] * ph;
          const rw = box[2] * pw;
          const rh = box[3] * ph;
          return (
            <Group
              key={entry.id}
              x={rx}
              y={ry}
              draggable
              onDragEnd={(event) =>
                props.onMoveCharacterRegion(entry.id, [
                  clamp01(Math.min((event.target.x() - px) / pw, 1 - box[2])),
                  clamp01(Math.min((event.target.y() - py) / ph, 1 - box[3])),
                  box[2],
                  box[3]
                ])
              }
            >
              <Rect width={rw} height={rh} stroke="#2f7d32" strokeWidth={4} dash={[12, 8]} />
              <Text x={6} y={6} text={entry.id} fontSize={22} fill="#2f7d32" />
            </Group>
          );
        })}
      {panel.dialogue.map((dialogue, index) => (
        <DialogueNode
          key={index}
          dialogue={dialogue}
          panelBox={[px, py, pw, ph]}
          onSelect={() => props.onSelectDialogue(index)}
          onMove={(box) => props.onMoveDialogue(index, box)}
          onMoveTail={(tip) => props.onMoveTail(index, tip)}
          showTail={props.showTail && props.selectedDialogue === index}
        />
      ))}
      {panel.sfx.map((sfx, index) => (
        <SfxNode
          key={index}
          sfx={sfx}
          panelBox={[px, py, pw, ph]}
          onSelect={() => props.onSelectSfx(index)}
          onMove={(box) => props.onMoveSfx(index, box)}
        />
      ))}
    </Group>
  );
}

function DialogueNode({
  dialogue,
  panelBox,
  onSelect,
  onMove,
  onMoveTail,
  showTail
}: {
  dialogue: Dialogue;
  panelBox: Box;
  onSelect: () => void;
  onMove: (box: Box) => void;
  onMoveTail: (tip: Point) => void;
  showTail: boolean;
}) {
  const [px, py, pw, ph] = panelBox;
  const box = dialogue.box ?? [0.1, 0.1, dialogue.vertical ? 0.3 : 0.6, dialogue.vertical ? 0.5 : 0.3];
  const bx = px + box[0] * pw;
  const by = py + box[1] * ph;
  const bw = box[2] * pw;
  const bh = box[3] * ph;
  const tip = dialogue.tail?.tip ?? [0.5, 0.95];
  const displayText = dialogue.vertical ? [...dialogue.text].join("\n") : dialogue.text;
  const burstPoints = Array.from({ length: 24 }, (_, index) => {
    const angle = (index / 24) * Math.PI * 2;
    const radius = index % 2 === 0 ? 0.5 : 0.4;
    return [bw / 2 + Math.cos(angle) * bw * radius, bh / 2 + Math.sin(angle) * bh * radius];
  }).flat();
  return (
    <Group>
      {/* tail未指定はレンダラーと同様に既定で表示する。 */}
      {(dialogue.tail?.enabled ?? true) && (
        <Line
          points={[bx + bw / 2, by + bh / 2, px + tip[0] * pw, py + tip[1] * ph]}
          stroke="#191919"
          strokeWidth={12}
          lineCap="round"
          listening={false}
        />
      )}
      <Group
        x={bx}
        y={by}
        draggable
        onClick={onSelect}
        onTap={onSelect}
        onDragEnd={(event) =>
          // 幅・高さを考慮してクランプし、box[0]+box[2]>1 等の無効JSONを防ぐ。
          onMove([
            clamp01(Math.min((event.target.x() - px) / pw, 1 - box[2])),
            clamp01(Math.min((event.target.y() - py) / ph, 1 - box[3])),
            box[2],
            box[3]
          ])
        }
      >
        {dialogue.balloon === "caption" ? (
          <Rect width={bw} height={bh} fill="#fcfcfa" stroke="#191919" strokeWidth={3} />
        ) : dialogue.balloon === "none" ? (
          <Rect width={bw} height={bh} fill="#ffffff22" />
        ) : dialogue.balloon === "burst" ? (
          <Line points={burstPoints} closed fill="#ffffff" stroke="#191919" strokeWidth={3} />
        ) : (
          <Ellipse
            x={bw / 2}
            y={bh / 2}
            radiusX={bw / 2}
            radiusY={bh / 2}
            fill="#ffffff"
            stroke="#191919"
            strokeWidth={dialogue.balloon === "cloud" ? 6 : 3}
          />
        )}
        <Text
          x={8}
          y={8}
          width={bw - 16}
          height={bh - 16}
          text={displayText}
          fontFamily="源暎アンチック, BIZ UDPGothic"
          fontSize={dialogue.font_size ?? 34}
          align="center"
          verticalAlign="middle"
          fill="#191919"
          wrap="char"
        />
      </Group>
      {showTail && (
        <Group
          x={px + tip[0] * pw}
          y={py + tip[1] * ph}
          draggable
          onDragEnd={(event) =>
            onMoveTail([clamp01((event.target.x() - px) / pw), clamp01((event.target.y() - py) / ph)])
          }
        >
          <Line points={[0, 0, -16, -16, 16, -16]} closed fill="#2b6cff" />
        </Group>
      )}
    </Group>
  );
}

function SfxNode({
  sfx,
  panelBox,
  onSelect,
  onMove
}: {
  sfx: Sfx;
  panelBox: Box;
  onSelect: () => void;
  onMove: (point: Point) => void;
}) {
  const [px, py, pw, ph] = panelBox;
  const pos = sfx.box ?? [0.5, 0.5];
  return (
    <Text
      x={px + pos[0] * pw}
      y={py + pos[1] * ph}
      text={sfx.vertical ? [...sfx.text].join("\n") : sfx.text}
      fontFamily="源暎アンチック, BIZ UDPGothic"
      fontSize={sfx.font_size ?? 54}
      fill={sfx.color ?? "#191919"}
      stroke={sfx.outline_color ?? "#ffffff"}
      strokeWidth={1}
      rotation={sfx.rotation ?? 0}
      draggable
      onClick={onSelect}
      onTap={onSelect}
      onDragEnd={(event) =>
        onMove([clamp01((event.target.x() - px) / pw), clamp01((event.target.y() - py) / ph)])
      }
    />
  );
}

function OverlayNode({
  overlay,
  version,
  selected,
  onSelect,
  onMove
}: {
  overlay: OverlayElement;
  version: number;
  selected: boolean;
  onSelect: () => void;
  onMove: (box: Box) => void;
}) {
  const image = useMaskedImage(overlay.asset, overlay.mask_asset, version);
  const width = overlay.box[2] * PAGE_W * overlay.scale;
  const height = overlay.box[3] * PAGE_H * overlay.scale;
  return (
    <Group
      x={overlay.box[0] * PAGE_W}
      y={overlay.box[1] * PAGE_H}
      opacity={overlay.opacity}
      draggable
      onClick={onSelect}
      onTap={onSelect}
      onDragEnd={(event) => {
        const x = clamp01(event.target.x() / PAGE_W);
        const y = clamp01(event.target.y() / PAGE_H);
        onMove([
          Math.min(x, 1 - overlay.box[2]),
          Math.min(y, 1 - overlay.box[3]),
          overlay.box[2],
          overlay.box[3]
        ]);
      }}
    >
      {image ? (
        <KonvaImage image={image} width={width} height={height} />
      ) : (
        <Rect width={width} height={height} fill="#7d88bd33" stroke="#5968aa" dash={[12, 8]} />
      )}
      {selected && <Rect width={width} height={height} stroke="#2b6cff" strokeWidth={5} />}
    </Group>
  );
}

function OverlayControls({
  overlay,
  panels,
  onChange,
  onUpload,
  onDelete
}: {
  overlay: OverlayElement;
  panels: Panel[];
  onChange: (patch: Partial<OverlayElement>) => void;
  onUpload: (kind: "asset" | "mask", file: File) => void;
  onDelete: () => void;
}) {
  return (
    <div className="overlay-controls">
      <strong>{overlay.id}</strong>
      <label>
        抽出元
        <select
          value={overlay.source_panel_id}
          onChange={(event) => onChange({ source_panel_id: event.target.value })}
        >
          {panels.map((panel) => (
            <option key={panel.panel_id}>{panel.panel_id}</option>
          ))}
        </select>
      </label>
      <label>
        画像
        <input
          type="file"
          accept="image/*"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onUpload("asset", file);
          }}
        />
      </label>
      <label>
        マスク
        <input
          type="file"
          accept="image/*"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onUpload("mask", file);
          }}
        />
      </label>
      <label>
        透明度
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={overlay.opacity}
          onChange={(event) => onChange({ opacity: Number(event.target.value) })}
        />
      </label>
      <label>
        倍率
        <input
          type="range"
          min={0.05}
          max={4}
          step={0.05}
          value={overlay.scale}
          onChange={(event) => onChange({ scale: Number(event.target.value) })}
        />
      </label>
      <label>
        レイヤー
        <select
          value={overlay.layer}
          onChange={(event) => onChange({ layer: event.target.value as "back" | "front" })}
        >
          <option value="back">背面</option>
          <option value="front">前面</option>
        </select>
      </label>
      <label>
        z-index
        <input
          type="number"
          value={overlay.z_index}
          onChange={(event) => onChange({ z_index: Number(event.target.value) })}
        />
      </label>
      <span>手前に戻すコマ</span>
      {panels.map((panel) => (
        <label key={panel.panel_id}>
          <input
            type="checkbox"
            checked={overlay.occluded_by_panel_ids.includes(panel.panel_id)}
            onChange={(event) =>
              onChange({
                occluded_by_panel_ids: event.target.checked
                  ? [...overlay.occluded_by_panel_ids, panel.panel_id]
                  : overlay.occluded_by_panel_ids.filter((id) => id !== panel.panel_id)
              })
            }
          />
          {panel.panel_id}
        </label>
      ))}
      <button className="danger" onClick={onDelete}>
        削除
      </button>
    </div>
  );
}

function CropControls({
  panel,
  onChange,
  onSubjectMode
}: {
  panel: Panel;
  onChange: (patch: Partial<Panel["generation"]>) => void;
  onSubjectMode: (mode: NonNullable<Panel["subject_mode"]>) => void;
}) {
  const generation = panel.generation ?? {};
  return (
    <fieldset>
      <legend>コマ: {panel.panel_id}</legend>
      <label>
        主題
        <select
          value={panel.subject_mode ?? "character_scene"}
          onChange={(event) => onSubjectMode(event.target.value as NonNullable<Panel["subject_mode"]>)}
        >
          {["character_scene", "reaction", "prop_insert", "hand_insert", "background"].map((mode) => (
            <option key={mode} value={mode}>
              {mode}
            </option>
          ))}
        </select>
      </label>
      <label>
        ズーム {(generation.crop_scale ?? 1).toFixed(2)}
        <input
          type="range"
          min={1}
          max={3}
          step={0.05}
          value={generation.crop_scale ?? 1}
          onChange={(event) => onChange({ crop_scale: Number(event.target.value) })}
        />
      </label>
      <label>
        左右 {(generation.crop_offset_x ?? 0).toFixed(2)}
        <input
          type="range"
          min={-1}
          max={1}
          step={0.05}
          value={generation.crop_offset_x ?? 0}
          onChange={(event) => onChange({ crop_offset_x: Number(event.target.value) })}
        />
      </label>
      <label>
        上下 {(generation.crop_offset_y ?? 0).toFixed(2)}
        <input
          type="range"
          min={-1}
          max={1}
          step={0.05}
          value={generation.crop_offset_y ?? 0}
          onChange={(event) => onChange({ crop_offset_y: Number(event.target.value) })}
        />
      </label>
    </fieldset>
  );
}

function DirectingControls({
  page,
  panel,
  showRegions,
  onShowRegions,
  onPageChange,
  onPanelChange,
  onCharacterRegion
}: {
  page: MangaPage;
  panel: Panel;
  showRegions: boolean;
  onShowRegions: (show: boolean) => void;
  onPageChange: (patch: Partial<MangaPage>) => void;
  onPanelChange: (patch: Partial<Panel>) => void;
  onCharacterRegion: (characterId: string, box: Box) => void;
}) {
  const textSafeArea = panel.text_safe_area ?? [0.55, 0.05, 0.35, 0.3];
  const patchTextSafeArea = (index: number, value: number) => {
    const next = [...textSafeArea] as Box;
    next[index] = clamp01(value);
    onPanelChange({ text_safe_area: normalizeBox(next) });
  };
  const patchRegion = (characterId: string, current: Box, index: number, value: number) => {
    const next = [...current] as Box;
    next[index] = clamp01(value);
    onCharacterRegion(characterId, normalizeBox(next));
  };

  return (
    <fieldset>
      <legend>漫画演出</legend>
      <label>
        ページ目的
        <input
          value={page.page_goal ?? ""}
          onChange={(event) => onPageChange({ page_goal: event.target.value })}
        />
      </label>
      <label>
        感情曲線
        <input
          value={(page.emotional_curve ?? []).join(" / ")}
          onChange={(event) =>
            onPageChange({
              emotional_curve: event.target.value
                .split("/")
                .map((item) => item.trim())
                .filter(Boolean)
            })
          }
        />
      </label>
      <label>
        役割
        <select value={panel.role ?? ""} onChange={(event) => onPanelChange({ role: event.target.value })}>
          <option value="">未設定</option>
          {PANEL_ROLES.map((role) => (
            <option key={role} value={role}>
              {role}
            </option>
          ))}
        </select>
      </label>
      <label>
        感情
        <input
          value={panel.emotion ?? ""}
          onChange={(event) => onPanelChange({ emotion: event.target.value })}
        />
      </label>
      <label>
        背景密度
        <select
          value={panel.background_density ?? ""}
          onChange={(event) => onPanelChange({ background_density: event.target.value })}
        >
          {BACKGROUND_DENSITIES.map((density) => (
            <option key={density || "empty"} value={density}>
              {density || "未設定"}
            </option>
          ))}
        </select>
      </label>
      <label>
        構図メモ
        <textarea
          value={panel.composition_notes ?? ""}
          onChange={(event) => onPanelChange({ composition_notes: event.target.value })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={panel.text_safe_area != null}
          onChange={(event) => onPanelChange({ text_safe_area: event.target.checked ? textSafeArea : null })}
        />
        写植予定領域を使う
      </label>
      {panel.text_safe_area && (
        <div className="settings-grid">
          {["x", "y", "幅", "高さ"].map((label, index) => (
            <label key={label}>
              {label}
              <input
                type="number"
                step="0.01"
                min="0"
                max="1"
                value={textSafeArea[index]}
                onChange={(event) => patchTextSafeArea(index, Number(event.target.value))}
              />
            </label>
          ))}
        </div>
      )}
      <label>
        <input
          type="checkbox"
          checked={showRegions}
          onChange={(event) => onShowRegions(event.target.checked)}
        />
        人物領域を表示
      </label>
      {(panel.character_layout ?? []).length > 0 && (
        <div className="region-editor">
          {(panel.character_layout ?? []).map((entry) => {
            const box = entry.region_box ?? [0.18, 0.08, 0.64, 0.84];
            return (
              <div key={entry.id}>
                <strong>{entry.id}</strong>
                <div className="settings-grid">
                  {["x", "y", "幅", "高さ"].map((label, index) => (
                    <label key={`${entry.id}-${label}`}>
                      {label}
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        max="1"
                        value={box[index]}
                        onChange={(event) => patchRegion(entry.id, box, index, Number(event.target.value))}
                      />
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </fieldset>
  );
}

function SfxControls({ sfx, onChange }: { sfx: Sfx; onChange: (patch: Partial<Sfx>) => void }) {
  return (
    <fieldset>
      <legend>擬音</legend>
      <label>
        文字
        <input
          type="text"
          maxLength={40}
          value={sfx.text}
          onChange={(event) => onChange({ text: event.target.value })}
        />
      </label>
      <small className="hint">英字の擬音は日本語表記にしてください（例: zip→ジッ）。</small>
      <label>
        style
        <select
          value={sfx.style ?? "small_handwritten"}
          onChange={(event) => onChange({ style: event.target.value })}
        >
          {SFX_STYLES.map((style) => (
            <option key={style} value={style}>
              {style}
            </option>
          ))}
        </select>
      </label>
      <label>
        <input
          type="checkbox"
          checked={sfx.vertical ?? false}
          onChange={(event) => onChange({ vertical: event.target.checked })}
        />{" "}
        縦書き
      </label>
      <label>
        文字サイズ
        <input
          type="number"
          min={12}
          max={240}
          value={sfx.font_size ?? 54}
          onChange={(event) => onChange({ font_size: Number(event.target.value) })}
        />
      </label>
    </fieldset>
  );
}

function BalloonControls({
  dialogue,
  onChange
}: {
  dialogue: Dialogue;
  onChange: (patch: Partial<Dialogue>) => void;
}) {
  return (
    <fieldset>
      <legend>吹き出し</legend>
      <label>
        種別
        <select
          value={dialogue.balloon}
          disabled={dialogue.balloon_auto ?? false}
          onChange={(event) =>
            onChange({ balloon: event.target.value as Dialogue["balloon"], balloon_auto: false })
          }
        >
          {BALLOON_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
      </label>
      <label title="種別(kind)に応じて吹き出し形状を自動選択します。手動で形状を選ぶと解除されます。">
        <input
          type="checkbox"
          checked={dialogue.balloon_auto ?? false}
          onChange={(event) =>
            onChange(
              event.target.checked
                ? {
                    balloon_auto: true,
                    // ON時は現在のkind既定形状へ即更新し、表示・プレビューのずれを防ぐ。
                    balloon: KIND_DEFAULT_BALLOON[dialogue.kind ?? "speech"] ?? "oval"
                  }
                : { balloon_auto: false }
            )
          }
        />
        形状をkindに自動追従
      </label>
      <label>
        <input
          type="checkbox"
          checked={dialogue.vertical ?? true}
          onChange={(event) => onChange({ vertical: event.target.checked })}
        />{" "}
        縦書き
      </label>
      <label>
        文字サイズ
        <input
          type="number"
          min={10}
          max={96}
          placeholder="プロジェクト既定"
          value={dialogue.font_size ?? ""}
          onChange={(event) =>
            onChange({ font_size: event.target.value ? Number(event.target.value) : null })
          }
        />
      </label>
      <label>
        縮小下限
        <input
          type="number"
          min={8}
          max={96}
          placeholder="プロジェクト既定"
          value={dialogue.min_font_size ?? ""}
          onChange={(event) =>
            onChange({ min_font_size: event.target.value ? Number(event.target.value) : null })
          }
        />
      </label>
      <label>
        最大行・列数
        <input
          type="number"
          min={1}
          max={20}
          value={dialogue.max_lines}
          onChange={(event) => onChange({ max_lines: Number(event.target.value) })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={dialogue.tail?.enabled ?? true}
          onChange={(event) =>
            onChange({
              tail: {
                ...(dialogue.tail ?? { tip: [0.5, 0.95], base: 0.5, width: 0.16 }),
                enabled: event.target.checked
              }
            })
          }
        />{" "}
        しっぽを表示
      </label>
      <p className="hint">しっぽ先端は吹き出し選択中に青い三角をドラッグ。</p>
    </fieldset>
  );
}
