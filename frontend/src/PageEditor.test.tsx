import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { forwardRef, useImperativeHandle, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MangaProject } from "./App";
import { PageEditor } from "./PageEditor";

type MockNodeProps = {
  children?: ReactNode;
  text?: string;
  onClick?: () => void;
  onTap?: () => void;
};

vi.mock("react-konva", () => {
  const Container = ({ children }: MockNodeProps) => <div>{children}</div>;
  const Rect = forwardRef<HTMLButtonElement, MockNodeProps>(({ children, onClick }, ref) => (
    <button type="button" data-testid="konva-rect" ref={ref} onClick={onClick}>
      {children}
    </button>
  ));
  const Transformer = forwardRef<object, MockNodeProps>((_props, ref) => {
    useImperativeHandle(ref, () => ({ nodes: () => undefined, getLayer: () => null }));
    return null;
  });
  return {
    Stage: Container,
    Layer: Container,
    Group: Container,
    Ellipse: Container,
    Line: Container,
    Image: Container,
    Rect,
    Text: ({ text }: MockNodeProps) => <span>{text}</span>,
    Transformer
  };
});

function sampleManga(): MangaProject {
  return {
    title: "テスト本",
    work_name: "テスト作品",
    premise: "",
    target_pages: 4,
    common_positive_prompt: "",
    common_negative_prompt: "",
    characters: [],
    locations: [],
    workflow_presets: [],
    active_workflow_preset_id: null,
    reading_direction: "rtl",
    pages: [
      {
        page: 1,
        theme: "導入",
        layout_template: "one",
        layout_family: "establish",
        layout_locked: false,
        reading_order: ["p01_01"],
        overlay_elements: [],
        render_status: "pending",
        rendered_at: null,
        panels: [
          {
            panel_id: "p01_01",
            bbox: [0.05, 0.05, 0.9, 0.9],
            shot: "wide",
            role: "establish",
            emotion: "",
            background_density: "",
            composition_notes: "",
            text_safe_area: null,
            camera: "",
            location_id: "",
            characters: ["mika"],
            character_layout: [
              {
                id: "mika",
                position: "center",
                expression: "",
                action: "",
                region_box: [0.2, 0.1, 0.5, 0.8]
              }
            ],
            prompt: "",
            image_asset: null,
            image_candidates: [],
            selected_candidate_id: null,
            control_references: [],
            dialogue: [
              {
                speaker: "美嘉",
                text: "テスト台詞",
                balloon: "oval",
                position: "upper_right",
                box: [0.1, 0.1, 0.4, 0.3],
                font_size: 30,
                max_lines: 4,
                vertical: true,
                tail: { enabled: true, tip: [0.5, 0.8], base: 0.5, width: 0.16 }
              }
            ],
            sfx: [],
            generation: {
              backend: "stub",
              prompt: "",
              negative_prompt: "",
              seed: 1,
              workflow_id: null,
              prompt_id: null,
              width: 768,
              height: 1024,
              fit_mode: "cover",
              crop_anchor: "center",
              text_policy: "no_text",
              model_notes: "",
              status: "pending",
              message: "",
              loras: [],
              reference_images: [],
              workflow_preset_id: null,
              workflow_preset: null
            }
          }
        ]
      }
    ]
  };
}

describe("PageEditor", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("overlayを追加・編集し、保存できる", async () => {
    const onChange = vi.fn();
    const onSave = vi.fn();
    render(
      <PageEditor
        projectId="project"
        revision={0}
        manga={sampleManga()}
        pageNumber={1}
        assetVersion={1}
        busy={false}
        onChange={onChange}
        onSave={onSave}
        onSuggest={vi.fn()}
        onOverlayUpload={vi.fn()}
        setMessage={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "追加" }));
    expect(onChange).toHaveBeenCalled();
    expect(screen.getByText(/overlay_/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "保存（レイアウト確定）" }));
    expect(onSave).toHaveBeenCalled();
  });

  it("プリフライト結果を表示する", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true, errors: [], warnings: [] }))
    );
    render(
      <PageEditor
        projectId="project"
        revision={0}
        manga={sampleManga()}
        pageNumber={1}
        assetVersion={1}
        busy={false}
        onChange={vi.fn()}
        onSave={vi.fn()}
        onSuggest={vi.fn()}
        onOverlayUpload={vi.fn()}
        setMessage={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() => expect(screen.getByText("問題は見つかりませんでした")).toBeVisible());
  });

  it("漫画レビューの警告から対象コマを選び演出情報を編集する", async () => {
    const onChange = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          errors: [],
          warnings: [
            {
              level: "warning",
              code: "shot_repetition",
              message: "同じ画角のコマが3つ続いています",
              page: 1,
              panel_id: "p01_01",
              category: "rhythm",
              suggestion: "画角を変えてください",
              fixable: false
            }
          ]
        })
      )
    );
    render(
      <PageEditor
        projectId="project"
        revision={0}
        manga={sampleManga()}
        pageNumber={1}
        assetVersion={1}
        busy={false}
        onChange={onChange}
        onSave={vi.fn()}
        onSuggest={vi.fn()}
        onOverlayUpload={vi.fn()}
        setMessage={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() => expect(screen.getByText("画角を変えてください")).toBeVisible());
    fireEvent.change(screen.getByLabelText("レビュー分類"), { target: { value: "rhythm" } });
    fireEvent.click(screen.getByRole("button", { name: /同じ画角/ }));

    fireEvent.change(screen.getByLabelText("ページ目的"), { target: { value: "動揺を見せる" } });
    fireEvent.change(screen.getByLabelText("役割"), { target: { value: "emotional_peak" } });
    fireEvent.change(screen.getByLabelText("背景密度"), { target: { value: "light" } });
    fireEvent.click(screen.getByLabelText("人物領域を表示"));

    expect(onChange).toHaveBeenCalled();
  });

  it("選択中のレビュー分類が消えたら全件表示に戻す", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: false,
            errors: [
              {
                level: "error",
                code: "sfx_english_text",
                message: "英字擬音があります",
                page: 1,
                panel_id: "p01_01",
                category: "sfx",
                fixable: false
              }
            ],
            warnings: []
          })
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: false,
            errors: [],
            warnings: [
              {
                level: "warning",
                code: "shot_repetition",
                message: "画角が単調です",
                page: 1,
                panel_id: "p01_01",
                category: "rhythm",
                fixable: false
              }
            ]
          })
        )
      );
    render(
      <PageEditor
        projectId="project"
        revision={0}
        manga={sampleManga()}
        pageNumber={1}
        assetVersion={1}
        busy={false}
        onChange={vi.fn()}
        onSave={vi.fn()}
        onSuggest={vi.fn()}
        onOverlayUpload={vi.fn()}
        setMessage={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /英字擬音/ })).toBeVisible());
    fireEvent.change(screen.getByLabelText("レビュー分類"), { target: { value: "sfx" } });

    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /画角が単調/ })).toBeVisible());
    expect(screen.getByLabelText("レビュー分類")).toHaveValue("all");
  });

  it("漫画レビューのSFXエラーから擬音テキストを修正する", async () => {
    const onChange = vi.fn();
    const manga = sampleManga();
    manga.pages[0].panels[0].sfx = [
      {
        text: "zip",
        position: "center",
        style: "small_handwritten",
        box: null,
        font_size: 54,
        rotation: 0,
        color: "#191919",
        outline_color: "#FFFFFF",
        outline_width: 4,
        vertical: false,
        layer: "above"
      }
    ];
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: false,
          errors: [
            {
              level: "error",
              code: "sfx_english_text",
              message: "英字の擬音「zip」が日本語へ変換されていません",
              page: 1,
              panel_id: "p01_01",
              category: "sfx",
              suggestion: "擬音は日本語表記にしてください（例: bang→バン）",
              fixable: false
            }
          ],
          warnings: []
        })
      )
    );
    render(
      <PageEditor
        projectId="project"
        revision={0}
        manga={manga}
        pageNumber={1}
        assetVersion={1}
        busy={false}
        onChange={onChange}
        onSave={vi.fn()}
        onSuggest={vi.fn()}
        onOverlayUpload={vi.fn()}
        setMessage={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /英字の擬音/ })).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: /英字の擬音/ }));

    // SFXのissueをクリックすると擬音編集欄が開き、文字を打ち直せる。
    const textInput = screen.getByLabelText("文字");
    expect(textInput).toHaveValue("zip");
    fireEvent.change(textInput, { target: { value: "ジッ" } });
    fireEvent.change(screen.getByLabelText("style"), { target: { value: "impact" } });
    fireEvent.click(screen.getByLabelText("縦書き"));

    expect(onChange).toHaveBeenCalled();
  });
});
