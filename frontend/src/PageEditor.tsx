import { useEffect, useMemo, useRef, useState } from "react";
import { Ellipse, Group, Image as KonvaImage, Layer, Line, Rect, Stage, Text, Transformer } from "react-konva";
import type Konva from "konva";

// ページ実寸（rendererと一致）。
const PAGE_W = 1200;
const PAGE_H = 1700;
const DISPLAY_W = 460;
const SCALE = DISPLAY_W / PAGE_W;
const SNAP = 0.01; // 1%グリッドへスナップ

const LAYOUT_FAMILIES = ["establish", "dialogue", "reveal", "action", "punchline", "silent", "montage"];
const BALLOON_KINDS = ["oval", "cloud", "burst", "caption", "none"];

type AnyManga = any;

type Props = {
  projectId: string;
  manga: AnyManga;
  pageNumber: number;
  assetVersion: number;
  busy: boolean;
  onChange: (manga: AnyManga) => void;
  onSave: () => Promise<void> | void;
  onSuggest: (family: string | null) => Promise<void> | void;
  setMessage: (text: string) => void;
};

function useAssetImage(asset: string | null, version: number): HTMLImageElement | null {
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    if (!asset) {
      setImg(null);
      return;
    }
    const image = new window.Image();
    image.src = `/api/assets/${asset}?v=${version}`;
    image.onload = () => setImg(image);
    return () => {
      image.onload = null;
    };
  }, [asset, version]);
  return img;
}

const snap = (value: number) => Math.round(value / SNAP) * SNAP;
const clamp01 = (value: number) => Math.max(0, Math.min(1, value));

export function PageEditor({ projectId, manga, pageNumber, assetVersion, busy, onChange, onSave, onSuggest, setMessage }: Props) {
  const page = manga?.pages?.find((item: any) => item.page === pageNumber) ?? null;
  const [selection, setSelection] = useState<{ panelId: string; kind: "panel" | "dialogue" | "sfx"; index: number } | null>(null);
  const [family, setFamily] = useState<string>("");
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

  const mutatePage = (mutator: (page: any) => void) => {
    const next = structuredClone(manga);
    const target = next.pages.find((item: any) => item.page === pageNumber);
    if (!target) return;
    mutator(target);
    onChange(next);
  };

  const updatePanelBbox = (panelId: string, bbox: [number, number, number, number]) => {
    mutatePage((target) => {
      const panel = target.panels.find((item: any) => item.panel_id === panelId);
      if (panel) panel.bbox = bbox;
    });
  };

  const renumberReadingOrder = () => {
    if (!page) return;
    const rtl = manga.reading_direction !== "ltr";
    const ordered = [...page.panels]
      .map((panel: any, index: number) => ({ panel, index }))
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
    return found >= 0 ? found + 1 : page?.panels.findIndex((p: any) => p.panel_id === panelId) + 1;
  };

  const selectedPanel = useMemo(
    () => (selection ? page?.panels.find((p: any) => p.panel_id === selection.panelId) ?? null : null),
    [selection, page]
  );

  if (!page) return <p className="hint">ページがありません。先にネームを生成してください。</p>;

  return (
    <div className="page-editor">
      <div className="page-editor-canvas">
        <Stage width={PAGE_W * SCALE} height={PAGE_H * SCALE} style={{ background: "#f8f8f4", border: "1px solid #ccc" }}
          onMouseDown={(event) => {
            if (event.target === event.target.getStage()) setSelection(null);
          }}
        >
          <Layer scaleX={SCALE} scaleY={SCALE}>
            {page.panels.map((panel: any) => (
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
                onSelectDialogue={(index: number) => setSelection({ panelId: panel.panel_id, kind: "dialogue", index })}
                onSelectSfx={(index: number) => setSelection({ panelId: panel.panel_id, kind: "sfx", index })}
                onMoveDialogue={(index: number, box: [number, number, number, number]) =>
                  mutatePage((target) => {
                    const p = target.panels.find((it: any) => it.panel_id === panel.panel_id);
                    if (p && p.dialogue[index]) p.dialogue[index].box = box;
                  })
                }
                onMoveTail={(index: number, tip: [number, number]) =>
                  mutatePage((target) => {
                    const p = target.panels.find((it: any) => it.panel_id === panel.panel_id);
                    if (p && p.dialogue[index]) {
                      const tail = p.dialogue[index].tail ?? { enabled: true, base: 0.5, width: 0.16 };
                      p.dialogue[index].tail = { ...tail, tip };
                    }
                  })
                }
                onMoveSfx={(index: number, box: [number, number]) =>
                  mutatePage((target) => {
                    const p = target.panels.find((it: any) => it.panel_id === panel.panel_id);
                    if (p && p.sfx[index]) p.sfx[index].box = box;
                  })
                }
                showTail={selection?.kind === "dialogue" && selection.panelId === panel.panel_id}
                selectedDialogue={selection?.kind === "dialogue" && selection.panelId === panel.panel_id ? selection.index : -1}
              />
            ))}
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
          <span className="hint">{page.layout_family || "未設定"} {page.layout_locked ? "🔒" : ""}</span>
        </div>

        <fieldset>
          <legend>レイアウト再提案</legend>
          <select value={family} onChange={(event) => setFamily(event.target.value)}>
            <option value="">自動（隣接ページと差別化）</option>
            {LAYOUT_FAMILIES.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
          <button disabled={busy} onClick={() => onSuggest(family || null)}>このページを再レイアウト</button>
          <button disabled={busy} onClick={renumberReadingOrder}>読み順を振り直す</button>
        </fieldset>

        {selectedPanel && selection?.kind === "panel" && (
          <CropControls
            panel={selectedPanel}
            onChange={(generation: any) =>
              mutatePage((target) => {
                const p = target.panels.find((it: any) => it.panel_id === selectedPanel.panel_id);
                if (p) p.generation = { ...p.generation, ...generation };
              })
            }
            onSubjectMode={(mode: string) =>
              mutatePage((target) => {
                const p = target.panels.find((it: any) => it.panel_id === selectedPanel.panel_id);
                if (p) p.subject_mode = mode;
              })
            }
          />
        )}

        {selection?.kind === "dialogue" && selectedPanel?.dialogue[selection.index] && (
          <BalloonControls
            dialogue={selectedPanel.dialogue[selection.index]}
            onChange={(patch: any) =>
              mutatePage((target) => {
                const p = target.panels.find((it: any) => it.panel_id === selectedPanel.panel_id);
                if (p && p.dialogue[selection.index]) p.dialogue[selection.index] = { ...p.dialogue[selection.index], ...patch };
              })
            }
          />
        )}

        <div className="editor-row">
          <button className="primary" disabled={busy} onClick={() => {
            mutatePage((target) => { target.layout_locked = true; });
            void onSave();
          }}>保存（レイアウト確定）</button>
        </div>
        <p className="hint">パネルをドラッグで移動・四隅で拡縮（1%グリッドにスナップ）。吹き出し・SFXもドラッグで移動できます。</p>
      </div>
    </div>
  );
}

function PanelNode(props: any) {
  const { panel, version, readingNumber, selected, registerRef, onSelect, onBbox } = props;
  const image = useAssetImage(panel.image_asset, version);
  const [px, py, pw, ph] = panel.bbox.map((value: number, i: number) => value * (i % 2 === 0 ? PAGE_W : PAGE_H));

  const handleTransform = (node: Konva.Rect) => {
    const scaleX = node.scaleX();
    const scaleY = node.scaleY();
    node.scaleX(1);
    node.scaleY(1);
    const bbox: [number, number, number, number] = [
      clamp01(snap(node.x() / PAGE_W)),
      clamp01(snap(node.y() / PAGE_H)),
      clamp01(snap((node.width() * scaleX) / PAGE_W)),
      clamp01(snap((node.height() * scaleY) / PAGE_H)),
    ];
    onBbox(bbox);
  };

  const cover = useMemo(() => {
    if (!image) return null;
    const scale = Math.max(pw / image.width, ph / image.height) * (panel.generation?.crop_scale ?? 1);
    const drawW = image.width * scale;
    const drawH = image.height * scale;
    const extraX = Math.max(0, drawW - pw);
    const extraY = Math.max(0, drawH - ph);
    const fracX = 0.5 + (panel.generation?.crop_offset_x ?? 0) * 0.5;
    const fracY = 0.5 + (panel.generation?.crop_offset_y ?? 0) * 0.5;
    return { drawW, drawH, offsetX: extraX * clamp01(fracX), offsetY: extraY * clamp01(fracY) };
  }, [image, pw, ph, panel.generation?.crop_scale, panel.generation?.crop_offset_x, panel.generation?.crop_offset_y]);

  return (
    <Group>
      <Group clipX={px} clipY={py} clipWidth={pw} clipHeight={ph}>
        {image && cover && (
          <KonvaImage image={image} x={px - cover.offsetX} y={py - cover.offsetY} width={cover.drawW} height={cover.drawH} />
        )}
      </Group>
      <Rect
        ref={registerRef}
        x={px}
        y={py}
        width={pw}
        height={ph}
        stroke={selected ? "#2b6cff" : "#141414"}
        strokeWidth={selected ? 6 : 4}
        draggable
        onClick={onSelect}
        onTap={onSelect}
        onDragEnd={(event) => onBbox([clamp01(snap(event.target.x() / PAGE_W)), clamp01(snap(event.target.y() / PAGE_H)), panel.bbox[2], panel.bbox[3]])}
        onTransformEnd={(event) => handleTransform(event.target as Konva.Rect)}
      />
      <Group>
        <Rect x={px + 8} y={py + 8} width={48} height={36} fill="#000000aa" cornerRadius={6} listening={false} />
        <Text x={px + 8} y={py + 14} width={48} text={String(readingNumber)} align="center" fontSize={24} fill="#fff" listening={false} />
      </Group>
      {panel.dialogue.map((dialogue: any, index: number) => (
        <DialogueNode
          key={index}
          dialogue={dialogue}
          panelBox={[px, py, pw, ph]}
          onSelect={() => props.onSelectDialogue(index)}
          onMove={(box: any) => props.onMoveDialogue(index, box)}
          onMoveTail={(tip: any) => props.onMoveTail(index, tip)}
          showTail={props.showTail && props.selectedDialogue === index}
        />
      ))}
      {panel.sfx.map((sfx: any, index: number) => (
        <SfxNode key={index} sfx={sfx} panelBox={[px, py, pw, ph]} onSelect={() => props.onSelectSfx(index)} onMove={(box: any) => props.onMoveSfx(index, box)} />
      ))}
    </Group>
  );
}

function DialogueNode({ dialogue, panelBox, onSelect, onMove, onMoveTail, showTail }: any) {
  const [px, py, pw, ph] = panelBox;
  const box = dialogue.box ?? [0.1, 0.1, dialogue.vertical ? 0.3 : 0.6, dialogue.vertical ? 0.5 : 0.3];
  const bx = px + box[0] * pw;
  const by = py + box[1] * ph;
  const bw = box[2] * pw;
  const bh = box[3] * ph;
  const tip = dialogue.tail?.tip ?? [0.5, 0.95];
  return (
    <Group>
      <Group
        x={bx}
        y={by}
        draggable
        onClick={onSelect}
        onTap={onSelect}
        onDragEnd={(event) =>
          onMove([clamp01((event.target.x() - px) / pw), clamp01((event.target.y() - py) / ph), box[2], box[3]])
        }
      >
        {dialogue.balloon === "caption" ? (
          <Rect width={bw} height={bh} fill="#fcfcfa" stroke="#191919" strokeWidth={3} />
        ) : dialogue.balloon === "none" ? (
          <Rect width={bw} height={bh} fill="#ffffff22" />
        ) : (
          <Ellipse x={bw / 2} y={bh / 2} radiusX={bw / 2} radiusY={bh / 2} fill="#ffffff" stroke="#191919" strokeWidth={3} dash={dialogue.balloon === "cloud" ? [10, 6] : undefined} />
        )}
        <Text x={6} y={6} width={bw - 12} height={bh - 12} text={dialogue.text} fontSize={26} fill="#191919" wrap="char" />
      </Group>
      {showTail && (
        <Group
          x={px + tip[0] * pw}
          y={py + tip[1] * ph}
          draggable
          onDragEnd={(event) => onMoveTail([clamp01((event.target.x() - px) / pw), clamp01((event.target.y() - py) / ph)])}
        >
          <Line points={[0, 0, -16, -16, 16, -16]} closed fill="#2b6cff" />
        </Group>
      )}
    </Group>
  );
}

function SfxNode({ sfx, panelBox, onSelect, onMove }: any) {
  const [px, py, pw, ph] = panelBox;
  const pos = sfx.box ?? [0.5, 0.5];
  return (
    <Text
      x={px + pos[0] * pw}
      y={py + pos[1] * ph}
      text={sfx.text}
      fontSize={sfx.font_size ?? 54}
      fill={sfx.color ?? "#191919"}
      stroke={sfx.outline_color ?? "#ffffff"}
      strokeWidth={1}
      rotation={sfx.rotation ?? 0}
      draggable
      onClick={onSelect}
      onTap={onSelect}
      onDragEnd={(event) => onMove([clamp01((event.target.x() - px) / pw), clamp01((event.target.y() - py) / ph)])}
    />
  );
}

function CropControls({ panel, onChange, onSubjectMode }: any) {
  const generation = panel.generation ?? {};
  return (
    <fieldset>
      <legend>コマ: {panel.panel_id}</legend>
      <label>主題
        <select value={panel.subject_mode ?? "character_scene"} onChange={(event) => onSubjectMode(event.target.value)}>
          {["character_scene", "reaction", "prop_insert", "hand_insert", "background"].map((mode) => (
            <option key={mode} value={mode}>{mode}</option>
          ))}
        </select>
      </label>
      <label>ズーム {(generation.crop_scale ?? 1).toFixed(2)}
        <input type="range" min={1} max={3} step={0.05} value={generation.crop_scale ?? 1}
          onChange={(event) => onChange({ crop_scale: Number(event.target.value) })} />
      </label>
      <label>左右 {(generation.crop_offset_x ?? 0).toFixed(2)}
        <input type="range" min={-1} max={1} step={0.05} value={generation.crop_offset_x ?? 0}
          onChange={(event) => onChange({ crop_offset_x: Number(event.target.value) })} />
      </label>
      <label>上下 {(generation.crop_offset_y ?? 0).toFixed(2)}
        <input type="range" min={-1} max={1} step={0.05} value={generation.crop_offset_y ?? 0}
          onChange={(event) => onChange({ crop_offset_y: Number(event.target.value) })} />
      </label>
    </fieldset>
  );
}

function BalloonControls({ dialogue, onChange }: any) {
  return (
    <fieldset>
      <legend>吹き出し</legend>
      <label>種別
        <select value={dialogue.balloon} onChange={(event) => onChange({ balloon: event.target.value })}>
          {BALLOON_KINDS.map((kind) => (
            <option key={kind} value={kind}>{kind}</option>
          ))}
        </select>
      </label>
      <label>
        <input type="checkbox" checked={dialogue.vertical ?? true} onChange={(event) => onChange({ vertical: event.target.checked })} /> 縦書き
      </label>
      <label>
        <input type="checkbox" checked={dialogue.tail?.enabled ?? true}
          onChange={(event) => onChange({ tail: { ...(dialogue.tail ?? { tip: [0.5, 0.95], base: 0.5, width: 0.16 }), enabled: event.target.checked } })} /> しっぽを表示
      </label>
      <p className="hint">しっぽ先端は吹き出し選択中に青い三角をドラッグ。</p>
    </fieldset>
  );
}
