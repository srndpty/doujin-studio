/* eslint-disable jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */
// react-konvaのモックでクリックを通すため、テスト専用にa11yルールを無効化する。
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { forwardRef, useImperativeHandle, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MangaProject, Panel } from "./App";
import { PageEditor } from "./PageEditor";

type KonvaProps = {
  children?: ReactNode;
  text?: string;
  onClick?: () => void;
  onTap?: () => void;
};

// クリックを通すモック。Group等はdiv、Rectはbutton、Textはspanにして
// DOMのバブリングで onClick（選択）を発火できるようにする。
vi.mock("react-konva", () => {
  const Pass = ({ children, onClick }: KonvaProps) => <div onClick={onClick}>{children}</div>;
  const Rect = forwardRef<HTMLButtonElement, KonvaProps>(({ children, onClick }, ref) => (
    <button type="button" data-testid="konva-rect" ref={ref} onClick={onClick}>
      {children}
    </button>
  ));
  Rect.displayName = "Rect";
  const Transformer = forwardRef<object, KonvaProps>((_props, ref) => {
    useImperativeHandle(ref, () => ({ nodes: () => undefined, getLayer: () => null }));
    return null;
  });
  return {
    Stage: ({ children }: KonvaProps) => <div>{children}</div>,
    Layer: Pass,
    Group: Pass,
    Ellipse: Pass,
    Line: Pass,
    Image: Pass,
    Rect,
    Text: ({ text, onClick }: KonvaProps) => <span onClick={onClick}>{text}</span>,
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
        reading_order: ["p01_01", "p01_02"],
        overlay_elements: [],
        render_status: "pending",
        rendered_at: null,
        panels: [
          panel("p01_01", [0.05, 0.05, 0.42, 0.4], "テスト台詞", true),
          panel("p01_02", [0.5, 0.05, 0.42, 0.4], "もう一つ", false)
        ]
      }
    ]
  };
}

function panel(id: string, bbox: [number, number, number, number], text: string, withSfx: boolean): Panel {
  return {
    panel_id: id,
    bbox,
    shot: "wide",
    camera: "",
    location_id: "",
    characters: [],
    prompt: "",
    image_asset: null,
    image_candidates: [],
    selected_candidate_id: null,
    control_references: [],
    subject_mode: "character_scene",
    role: "",
    emphasis: 2,
    dialogue: [
      {
        speaker: "美嘉",
        text,
        balloon: "oval",
        position: "upper_right",
        box: [0.1, 0.1, 0.4, 0.3],
        font_size: 30,
        min_font_size: 26,
        max_lines: 4,
        vertical: true,
        tail: { enabled: true, tip: [0.5, 0.8], base: 0.5, width: 0.16 }
      }
    ],
    sfx: withSfx
      ? [
          {
            text: "ドン",
            position: "center",
            style: "small_handwritten",
            box: [0.5, 0.5] as [number, number],
            font_size: 54,
            rotation: 0,
            color: "#191919",
            outline_color: "#ffffff",
            outline_width: 4,
            vertical: false,
            layer: "above"
          }
        ]
      : [],
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
      crop_scale: 1,
      crop_offset_x: 0,
      crop_offset_y: 0,
      focal_x: null,
      focal_y: null,
      text_policy: "no_text",
      model_notes: "",
      status: "pending",
      message: "",
      loras: [],
      reference_images: [],
      workflow_preset_id: null,
      workflow_preset: null
    }
  };
}

function setup(overrides: Partial<Parameters<typeof PageEditor>[0]> = {}) {
  const props = {
    projectId: "project",
    manga: sampleManga(),
    pageNumber: 1,
    assetVersion: 1,
    busy: false,
    onChange: vi.fn(),
    onSave: vi.fn(),
    onSuggest: vi.fn(),
    setMessage: vi.fn(),
    ...overrides
  };
  render(<PageEditor {...props} />);
  return props;
}

describe("PageEditor interactions", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("ページが無ければ案内を表示する", () => {
    setup({ pageNumber: 99 });
    expect(screen.getByText(/ページがありません/)).toBeVisible();
  });

  it("再レイアウトと読み順振り直しを呼ぶ", () => {
    const props = setup();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "montage" } });
    fireEvent.click(screen.getByRole("button", { name: "このページを再レイアウト" }));
    expect(props.onSuggest).toHaveBeenCalledWith("montage");
    fireEvent.click(screen.getByRole("button", { name: "読み順を振り直す" }));
    expect(props.onChange).toHaveBeenCalled();
    expect(props.setMessage).toHaveBeenCalledWith("読み順を位置から振り直しました");
  });

  it("コマを選択してcrop/主題を編集する", () => {
    const props = setup();
    fireEvent.click(screen.getAllByTestId("konva-rect")[0]);
    const crop = screen.getByText(/コマ: p01_01/).closest("fieldset") as HTMLElement;
    fireEvent.change(within(crop).getByLabelText("主題"), { target: { value: "prop_insert" } });
    fireEvent.change(within(crop).getByLabelText(/ズーム/), { target: { value: "2" } });
    fireEvent.change(within(crop).getByLabelText(/左右/), { target: { value: "0.5" } });
    fireEvent.change(within(crop).getByLabelText(/上下/), { target: { value: "-0.5" } });
    expect(props.onChange).toHaveBeenCalled();
  });

  it("吹き出しを選択して種別・縦書き・しっぽを編集する", () => {
    const props = setup();
    // 縦書きは文字間に改行が入るため正規化して一致させる。
    fireEvent.click(
      screen.getByText(
        (_content, element) =>
          element?.tagName === "SPAN" && (element.textContent ?? "").replace(/\s+/g, "") === "テスト台詞"
      )
    );
    const balloon = screen.getByText("吹き出し").closest("fieldset") as HTMLElement;
    fireEvent.change(within(balloon).getByLabelText("種別"), { target: { value: "burst" } });
    const checkboxes = within(balloon).getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]); // 縦書き
    fireEvent.click(checkboxes[1]); // しっぽ
    expect(props.onChange).toHaveBeenCalled();
  });

  it("オーバーフレームを追加・編集・アップロード・削除する", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ manga_json: sampleManga() })));
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: "追加" }));
    expect(props.onChange).toHaveBeenCalled();
    const controls = screen.getByText(/overlay_/).closest(".overlay-controls") as HTMLElement;
    fireEvent.change(within(controls).getByLabelText("抽出元"), { target: { value: "p01_02" } });
    fireEvent.change(within(controls).getByLabelText("透明度"), { target: { value: "0.5" } });
    fireEvent.change(within(controls).getByLabelText("倍率"), { target: { value: "2" } });
    fireEvent.change(within(controls).getByLabelText("レイヤー"), { target: { value: "back" } });
    fireEvent.change(within(controls).getByLabelText("z-index"), { target: { value: "3" } });
    fireEvent.click(within(controls).getAllByRole("checkbox")[0]);

    const file = new File(["x"], "a.png", { type: "image/png" });
    fireEvent.change(within(controls).getByLabelText("画像"), { target: { files: [file] } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [saveUrl, saveOptions] = fetchMock.mock.calls[0];
    expect(saveUrl).toBe("/api/projects/project/manga-json");
    expect(saveOptions?.method).toBe("PUT");
    const savedManga = JSON.parse(String(saveOptions?.body)) as MangaProject;
    expect(savedManga.pages[0].overlay_elements).toHaveLength(1);
    expect(String(fetchMock.mock.calls[1][0])).toContain("/overlays/overlay_");

    fireEvent.click(within(controls).getByRole("button", { name: "削除" }));
    expect(props.setMessage).toHaveBeenCalled();
  });

  it("オーバーフレームのアップロード失敗を通知する", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("ng", { status: 500 }));
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: "追加" }));
    const controls = screen.getByText(/overlay_/).closest(".overlay-controls") as HTMLElement;
    const file = new File(["x"], "a.png", { type: "image/png" });
    fireEvent.change(within(controls).getByLabelText("画像"), { target: { files: [file] } });
    await waitFor(() =>
      expect(props.setMessage).toHaveBeenCalledWith(expect.stringContaining("アップロードに失敗"))
    );
  });

  it("プリフライトの警告とエラーを表示する", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: false,
          errors: [{ level: "error", code: "x", message: "重大エラー" }],
          warnings: [{ level: "warning", code: "y", message: "注意点" }]
        })
      )
    );
    setup();
    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() => expect(screen.getByText(/重大エラー/)).toBeVisible());
    expect(screen.getByText(/注意点/)).toBeVisible();
  });

  it("プリフライト失敗時にメッセージを出す", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("ng", { status: 500 }));
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: "このページを検査" }));
    await waitFor(() =>
      expect(props.setMessage).toHaveBeenCalledWith(expect.stringContaining("プリフライトに失敗"))
    );
  });

  it("保存でlayout_lockedを立ててonSaveを呼ぶ", () => {
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: "保存（レイアウト確定）" }));
    expect(props.onSave).toHaveBeenCalled();
    const saved = (props.onSave as ReturnType<typeof vi.fn>).mock.calls[0][0] as MangaProject;
    expect(saved.pages[0].layout_locked).toBe(true);
  });

  it("左綴じ(ltr)でも読み順を振り直せる", () => {
    const manga = sampleManga();
    manga.reading_direction = "ltr";
    const props = setup({ manga });
    fireEvent.click(screen.getByRole("button", { name: "読み順を振り直す" }));
    expect(props.onChange).toHaveBeenCalled();
  });
});
