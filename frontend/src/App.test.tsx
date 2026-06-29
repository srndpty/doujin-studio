import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { forwardRef, useImperativeHandle, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { MangaProject } from "./manga-types";

// PageEditorはreact-konva(Canvas)を使うため、jsdomで描画できるようモック化する。
vi.mock("react-konva", () => {
  type MockNodeProps = { children?: ReactNode; text?: string; onClick?: () => void };
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

const manga: MangaProject = {
  title: "テスト本",
  work_name: "テスト作品",
  premise: "",
  target_pages: 4,
  reading_direction: "rtl",
  typography: {
    primary_font: "源暎アンチック",
    default_font_size: 34,
    min_font_size: 26,
    vertical_default: true
  },
  common_positive_prompt: "",
  common_negative_prompt: "",
  characters: [],
  locations: [],
  workflow_presets: [],
  active_workflow_preset_id: null,
  pages: []
};

function jsonResponse(value: unknown): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } })
  );
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

const statusResponses = (url: string): Promise<Response> | null => {
  if (url.endsWith("/production-status")) {
    return jsonResponse({
      project_id: "p1",
      status: "incomplete",
      adopted_panels: 0,
      total_panels: 1,
      rendered_pages: 0,
      total_pages: 1,
      pages: [],
      blockers: []
    });
  }
  if (url.endsWith("/generation-jobs")) return jsonResponse([]);
  if (url === "/api/comfyui/status") {
    return jsonResponse({
      backend: "stub",
      base_url: "",
      workflow_path: "",
      connected: false,
      workflow_exists: false,
      workflow_valid: false,
      missing_nodes: [],
      message: "stub"
    });
  }
  return null;
};

function mangaWithPanel(title: string, pageAsset: string | null = null): MangaProject {
  return {
    ...manga,
    title,
    pages: [
      {
        page: 1,
        theme: "導入",
        layout_template: "one",
        reading_order: ["p01_01"],
        overlay_elements: [],
        render_status: pageAsset ? "rendered" : "pending",
        rendered_at: null,
        render_asset: pageAsset,
        panels: [
          {
            panel_id: "p01_01",
            bbox: [0.05, 0.05, 0.9, 0.9],
            shot: "wide",
            camera: "",
            location_id: "",
            characters: [],
            prompt: "",
            image_asset: null,
            image_candidates: [],
            selected_candidate_id: null,
            control_references: [],
            dialogue: [],
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
  } as MangaProject;
}

function mangaWithTwoPages(title: string, firstAsset: string | null = null): MangaProject {
  const first = mangaWithPanel(title, firstAsset).pages[0];
  const second = {
    ...first,
    page: 2,
    reading_order: ["p02_01"],
    render_asset: null,
    panels: [{ ...first.panels[0], panel_id: "p02_01" }]
  };
  return { ...mangaWithPanel(title, firstAsset), pages: [first, second] } as MangaProject;
}

describe("AppのManga JSON保存", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("直接編集したjsonTextをツールバー保存で送信する", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        const body = JSON.parse(String(init.body)) as MangaProject;
        return jsonResponse({
          project: {
            id: "p1",
            title: body.title,
            work_name: body.work_name,
            revision: 1,
            manga_json: body
          },
          result: {}
        });
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "テスト本", work_name: "テスト作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        return jsonResponse({
          id: "p1",
          title: "テスト本",
          work_name: "テスト作品",
          revision: 0,
          manga_json: manga
        });
      }
      if (url.endsWith("/production-status")) {
        return jsonResponse({
          project_id: "p1",
          status: "incomplete",
          adopted_panels: 0,
          total_panels: 0,
          rendered_pages: 0,
          total_pages: 0,
          pages: [],
          blockers: []
        });
      }
      if (url.endsWith("/generation-jobs")) return jsonResponse([]);
      if (url === "/api/comfyui/status") {
        return jsonResponse({
          backend: "stub",
          base_url: "",
          workflow_path: "",
          connected: false,
          workflow_exists: false,
          workflow_valid: false,
          missing_nodes: [],
          message: "stub"
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("テスト本"));
    await screen.findByLabelText("本のタイトル");
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    expect(textarea).not.toBeNull();
    const edited = { ...manga, title: "JSONから変更" };
    fireEvent.change(textarea as HTMLTextAreaElement, { target: { value: JSON.stringify(edited) } });
    fireEvent.click(screen.getByTitle("タイトルと編集内容を保存"));

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(([, options]) => options?.method === "PUT");
      expect(putCall).toBeDefined();
      expect(JSON.parse(String(putCall?.[1]?.body)).title).toBe("JSONから変更");
      // 楽観ロック用にrevisionを必ず添える。
      expect(String(putCall?.[0])).toContain("revision=0");
    });
  });

  it("409時は最新を読み込み直し、未保存の編集で上書きしない", async () => {
    const latestManga = { ...manga, title: "他タブが保存した最新" };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        // 競合: サーバはCASで409を返す。
        return Promise.resolve(
          new Response(
            JSON.stringify({
              detail: "競合",
              code: "project_revision_conflict",
              expected_revision: 5,
              actual_revision: 7,
              project: {
                id: "p1",
                title: "他タブが保存した最新",
                work_name: "テスト作品",
                revision: 7,
                manga_json: latestManga
              }
            }),
            { status: 409, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "テスト本", work_name: "テスト作品", revision: 5, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        // 409後の再取得では最新(revision進行済み)を返す。
        return jsonResponse({
          id: "p1",
          title: "他タブが保存した最新",
          work_name: "テスト作品",
          revision: 7,
          manga_json: latestManga
        });
      }
      if (url.endsWith("/production-status")) {
        return jsonResponse({
          project_id: "p1",
          status: "incomplete",
          adopted_panels: 0,
          total_panels: 0,
          rendered_pages: 0,
          total_pages: 0,
          pages: [],
          blockers: []
        });
      }
      if (url.endsWith("/generation-jobs")) return jsonResponse([]);
      if (url === "/api/comfyui/status") {
        return jsonResponse({
          backend: "stub",
          base_url: "",
          workflow_path: "",
          connected: false,
          workflow_exists: false,
          workflow_valid: false,
          missing_nodes: [],
          message: "stub"
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("テスト本"));
    await screen.findByLabelText("本のタイトル");
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    const edited = { ...manga, title: "自分のローカル編集" };
    fireEvent.change(textarea as HTMLTextAreaElement, { target: { value: JSON.stringify(edited) } });
    fireEvent.click(screen.getByTitle("タイトルと編集内容を保存"));

    // 409後、最新内容が読み込み直され、ローカル編集は反映されない。
    await waitFor(() => {
      expect((textarea as HTMLTextAreaElement).value).toContain("他タブが保存した最新");
    });
    expect((textarea as HTMLTextAreaElement).value).not.toContain("自分のローカル編集");
    // PUTは1回だけ（自動で古い全文を再送して上書きしていない）。
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
    // 最新project採用時は派生状態も再取得する。
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/production-status")).length
    ).toBeGreaterThan(1);
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/generation-jobs")).length
    ).toBeGreaterThan(1);
  });

  it("プロジェクト切替後は", async () => {
    const p2Manga = { ...manga, title: "プロジェクト2" };
    let resolveCbz!: (response: Response) => void;
    const delayedCbz = new Promise<Response>((resolve) => {
      resolveCbz = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "POST" && url === "/api/projects/p1/export/folder?revision=0") {
        return delayedCbz;
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "テスト本", work_name: "作品1", revision: 0, updated_at: "2026-01-01" },
          { id: "p2", title: "プロジェクト2", work_name: "作品2", revision: 4, updated_at: "2026-01-02" }
        ]);
      }
      if (url === "/api/projects/p1") {
        return jsonResponse({
          id: "p1",
          title: "テスト本",
          work_name: "作品1",
          revision: 0,
          manga_json: manga
        });
      }
      if (url === "/api/projects/p2") {
        return jsonResponse({
          id: "p2",
          title: "プロジェクト2",
          work_name: "作品2",
          revision: 4,
          manga_json: p2Manga
        });
      }
      if (url.endsWith("/production-status")) {
        return jsonResponse({
          project_id: url.includes("/p2/") ? "p2" : "p1",
          status: "incomplete",
          adopted_panels: 0,
          total_panels: 0,
          rendered_pages: 0,
          total_pages: 0,
          pages: [],
          blockers: []
        });
      }
      if (url.endsWith("/generation-jobs")) return jsonResponse([]);
      if (url === "/api/comfyui/status") {
        return jsonResponse({
          backend: "stub",
          base_url: "",
          workflow_path: "",
          connected: false,
          workflow_exists: false,
          workflow_valid: false,
          missing_nodes: [],
          message: "stub"
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("テスト本"));
    await screen.findByDisplayValue("テスト本");
    fireEvent.click(screen.getByTitle("ページ画像とmanga.json・メタデータをフォルダへ書き出す"));
    fireEvent.click(screen.getByText("プロジェクト2"));
    await screen.findByDisplayValue("プロジェクト2");

    resolveCbz(
      new Response(
        JSON.stringify({
          project: {
            id: "p1",
            title: "遅延したプロジェクト1",
            work_name: "作品1",
            revision: 9,
            manga_json: { ...manga, title: "遅延したプロジェクト1" }
          },
          result: { folder_path: "p1/export", page_count: 4, warnings: [] }
        }),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    await waitFor(() => expect(screen.getByLabelText("本のタイトル")).toHaveValue("プロジェクト2"));
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    expect(textarea?.value).toContain("プロジェクト2");
    expect(textarea?.value).not.toContain("遅延したプロジェクト1");
  });

  it("同一projectの古いrevision応答が遅れて到着しても巻き戻さない", async () => {
    let resolveCbz!: (response: Response) => void;
    const delayedCbz = new Promise<Response>((resolve) => {
      resolveCbz = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "POST" && url.includes("/export/folder")) {
        return delayedCbz;
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "最新本", work_name: "作品", revision: 5, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        // 現在のUIはrevision 5を表示している。
        return jsonResponse({
          id: "p1",
          title: "最新本",
          work_name: "作品",
          revision: 5,
          manga_json: { ...manga, title: "最新本" }
        });
      }
      if (url.endsWith("/production-status")) {
        return jsonResponse({
          project_id: "p1",
          status: "incomplete",
          adopted_panels: 0,
          total_panels: 0,
          rendered_pages: 0,
          total_pages: 0,
          pages: [],
          blockers: []
        });
      }
      if (url.endsWith("/generation-jobs")) return jsonResponse([]);
      if (url === "/api/comfyui/status") {
        return jsonResponse({
          backend: "stub",
          base_url: "",
          workflow_path: "",
          connected: false,
          workflow_exists: false,
          workflow_valid: false,
          missing_nodes: [],
          message: "stub"
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("最新本"));
    await screen.findByDisplayValue("最新本");
    fireEvent.click(screen.getByTitle("ページ画像とmanga.json・メタデータをフォルダへ書き出す"));

    // 遅延CBZ応答は古いrevision 3のsnapshotを返す。
    resolveCbz(
      new Response(
        JSON.stringify({
          project: {
            id: "p1",
            title: "巻き戻し版",
            work_name: "作品",
            revision: 3,
            manga_json: { ...manga, title: "巻き戻し版" }
          },
          latest_revision: 5,
          result: { folder_path: "p1/export", page_count: 4, warnings: [] }
        }),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    // しばらく待っても、UIは新しいrevision 5のままで巻き戻らない。
    await new Promise((resolve) => setTimeout(resolve, 0));
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    expect(textarea?.value).toContain("最新本");
    expect(textarea?.value).not.toContain("巻き戻し版");
    expect(screen.getByLabelText("本のタイトル")).toHaveValue("最新本");
  });

  it("latest_revisionが新しい保存応答の後は、reload後のrevisionで後続renderを呼ぶ", async () => {
    let putDone = false;
    const renderUrls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        putDone = true;
        // 保存snapshotはrevision 1だが、DBは既にrevision 5まで進んでいる。
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 1,
            manga_json: mangaWithPanel("本")
          },
          latest_revision: 5,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderUrls.push(url);
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 6,
            manga_json: mangaWithPanel("本")
          },
          latest_revision: 6,
          result: { panel_id: "p01_01", page_asset: "p1/pages/page_001.png", warnings: [] }
        });
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "本", work_name: "作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        // reload(保存後)では最新revision 5を返す。
        return jsonResponse({
          id: "p1",
          title: "本",
          work_name: "作品",
          revision: putDone ? 5 : 0,
          manga_json: mangaWithPanel("本")
        });
      }
      return jsonResponse({});
    });

    render(<App />);
    fireEvent.click(await screen.findByText("本"));
    await screen.findByDisplayValue("本");
    fireEvent.click(screen.getByTitle("全ページをレンダリング"));

    // 保存応答のlatest_revision(5)で再同期した後、render-pageには5を送る（1ではない）。
    await waitFor(() => expect(renderUrls).toHaveLength(1));
    expect(renderUrls[0]).toContain("revision=5");
  });

  it("render途中で再同期したら古いworklistの後続renderを実行しない", async () => {
    let renderCount = 0;
    let firstRenderDone = false;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 1,
            manga_json: mangaWithTwoPages("本")
          },
          latest_revision: 1,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderCount += 1;
        firstRenderDone = true;
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 2,
            manga_json: mangaWithTwoPages("本", "p1/pages/stale-first.png")
          },
          latest_revision: 3,
          result: { panel_id: "p01_01", page_asset: "p1/pages/stale-first.png", warnings: [] }
        });
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "本", work_name: "作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        return jsonResponse({
          id: "p1",
          title: "本",
          work_name: "作品",
          revision: firstRenderDone ? 3 : 0,
          manga_json: mangaWithTwoPages("本", firstRenderDone ? "p1/pages/fresh-first.png" : null)
        });
      }
      return jsonResponse({});
    });

    render(<App />);
    fireEvent.click(await screen.findByText("本"));
    await screen.findByDisplayValue("本");
    fireEvent.click(screen.getByTitle("全ページをレンダリング"));

    await waitFor(() => expect(renderCount).toBe(1));
    await waitFor(() =>
      expect(
        screen.getByText("構成が更新されたためレンダリングを中断しました。再実行してください。")
      ).toBeInTheDocument()
    );
    expect(renderCount).toBe(1);
  });

  it("saveJsonDraftが未採用なら後続の生成APIを呼ばない", async () => {
    const delayedPut = deferredResponse();
    let putStarted = false;
    let generationCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        putStarted = true;
        return delayedPut.promise;
      }
      if (init?.method === "POST" && url.includes("/generation-jobs")) {
        generationCalls += 1;
        return jsonResponse({});
      }
      const status = statusResponses(url);
      if (status) return status;
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "プロジェクト1", work_name: "作品1", revision: 0, updated_at: "2026-01-01" },
          { id: "p2", title: "プロジェクト2", work_name: "作品2", revision: 4, updated_at: "2026-01-02" }
        ]);
      }
      if (url === "/api/projects/p1") {
        return jsonResponse({
          id: "p1",
          title: "プロジェクト1",
          work_name: "作品1",
          revision: 0,
          manga_json: mangaWithPanel("プロジェクト1")
        });
      }
      if (url === "/api/projects/p2") {
        return jsonResponse({
          id: "p2",
          title: "プロジェクト2",
          work_name: "作品2",
          revision: 4,
          manga_json: mangaWithPanel("プロジェクト2")
        });
      }
      return jsonResponse({});
    });

    render(<App />);
    fireEvent.click(await screen.findByText("プロジェクト1"));
    await screen.findByDisplayValue("プロジェクト1");
    fireEvent.click(await screen.findByRole("button", { name: "選択コマを生成" }));
    await waitFor(() => expect(putStarted).toBe(true));

    fireEvent.click(screen.getByText("プロジェクト2"));
    await screen.findByDisplayValue("プロジェクト2");
    delayedPut.resolve(
      new Response(
        JSON.stringify({
          project: {
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 1,
            manga_json: mangaWithPanel("プロジェクト1")
          },
          latest_revision: 1,
          result: {}
        }),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByLabelText("本のタイトル")).toHaveValue("プロジェクト2");
    expect(generationCalls).toBe(0);
  });

  it("p1再同期待ち中にp2へ切り替えたらp2の状態をp1応答で触らない", async () => {
    const delayedReload = deferredResponse();
    let p1GetCount = 0;
    let renderCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 1,
            manga_json: mangaWithPanel("プロジェクト1")
          },
          latest_revision: 2,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderCalls += 1;
        return jsonResponse({});
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "プロジェクト1", work_name: "作品1", revision: 0, updated_at: "2026-01-01" },
          { id: "p2", title: "プロジェクト2", work_name: "作品2", revision: 4, updated_at: "2026-01-02" }
        ]);
      }
      if (url === "/api/projects/p1") {
        p1GetCount += 1;
        if (p1GetCount === 1) {
          return jsonResponse({
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 0,
            manga_json: mangaWithPanel("プロジェクト1")
          });
        }
        return delayedReload.promise;
      }
      if (url === "/api/projects/p2") {
        return jsonResponse({
          id: "p2",
          title: "プロジェクト2",
          work_name: "作品2",
          revision: 4,
          manga_json: mangaWithPanel("プロジェクト2", "p2/pages/current.png")
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("プロジェクト1"));
    await screen.findByDisplayValue("プロジェクト1");
    fireEvent.click(screen.getByTitle("全ページをレンダリング"));
    await waitFor(() => expect(p1GetCount).toBe(2));

    fireEvent.click(screen.getByText("プロジェクト2"));
    await screen.findByDisplayValue("プロジェクト2");
    delayedReload.resolve(
      new Response(
        JSON.stringify({
          id: "p1",
          title: "プロジェクト1の最新",
          work_name: "作品1",
          revision: 2,
          manga_json: mangaWithPanel("プロジェクト1の最新", "p1/pages/latest.png")
        }),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByLabelText("本のタイトル")).toHaveValue("プロジェクト2");
    expect(renderCalls).toBe(0);
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    expect(textarea?.value).toContain("プロジェクト2");
    expect(textarea?.value).not.toContain("プロジェクト1の最新");
  });

  it("全ページ生成後のreload待機中にp2へ切り替えたらp2構成でp1をrenderしない", async () => {
    const delayedReload = deferredResponse();
    let p1GetCount = 0;
    let renderCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 1,
            manga_json: mangaWithPanel("プロジェクト1")
          },
          latest_revision: 1,
          result: {}
        });
      }
      if (init?.method === "POST" && url === "/api/projects/p1/generation-jobs?revision=1") {
        return jsonResponse({
          project: {
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 2,
            manga_json: mangaWithPanel("プロジェクト1")
          },
          latest_revision: 2,
          result: { jobs: [] }
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderCalls += 1;
        return jsonResponse({});
      }
      const status = statusResponses(url);
      if (status) return status;
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "プロジェクト1", work_name: "作品1", revision: 0, updated_at: "2026-01-01" },
          { id: "p2", title: "プロジェクト2", work_name: "作品2", revision: 4, updated_at: "2026-01-02" }
        ]);
      }
      if (url === "/api/projects/p1") {
        p1GetCount += 1;
        if (p1GetCount === 1) {
          return jsonResponse({
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 0,
            manga_json: mangaWithPanel("プロジェクト1")
          });
        }
        if (p1GetCount === 2) {
          return jsonResponse({
            id: "p1",
            title: "プロジェクト1",
            work_name: "作品1",
            revision: 3,
            manga_json: mangaWithPanel("プロジェクト1")
          });
        }
        return delayedReload.promise;
      }
      if (url === "/api/projects/p2") {
        return jsonResponse({
          id: "p2",
          title: "プロジェクト2",
          work_name: "作品2",
          revision: 4,
          manga_json: mangaWithPanel("プロジェクト2", "p2/pages/current.png")
        });
      }
      return jsonResponse({});
    });

    render(<App />);
    fireEvent.click(await screen.findByText("プロジェクト1"));
    await screen.findByDisplayValue("プロジェクト1");
    fireEvent.click(screen.getByTitle("全ページを生成"));
    await waitFor(() => expect(p1GetCount).toBe(3));

    fireEvent.click(screen.getByText("プロジェクト2"));
    await screen.findByDisplayValue("プロジェクト2");
    delayedReload.resolve(
      new Response(
        JSON.stringify({
          id: "p1",
          title: "プロジェクト1の最新",
          work_name: "作品1",
          revision: 3,
          manga_json: mangaWithPanel("プロジェクト1の最新")
        }),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByLabelText("本のタイトル")).toHaveValue("プロジェクト2");
    expect(renderCalls).toBe(0);
  });

  it("古いrevisionのrender応答はselected・JSON・pageAssetsを巻き戻さない", async () => {
    let generationStarted = false;
    let renderCalls = 0;
    const job = {
      id: "job-1",
      project_id: "p1",
      panel_id: "p01_01",
      candidate_count: 1,
      status: "queued",
      progress: 0,
      message: "生成中",
      node: null,
      prompt_id: null,
      candidate_ids: [],
      created_at: "2026-01-01",
      updated_at: "2026-01-01"
    };
    class ImmediateWebSocket {
      onmessage: ((event: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() {
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({ ...job, status: "done", progress: 100, message: "完了" })
          });
        }, 0);
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", ImmediateWebSocket);
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "rev11",
            work_name: "作品",
            revision: 1,
            manga_json: mangaWithPanel("rev11")
          },
          latest_revision: 1,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/generation-jobs")) {
        generationStarted = true;
        return jsonResponse({
          project: {
            id: "p1",
            title: "rev11",
            work_name: "作品",
            revision: 2,
            manga_json: mangaWithPanel("rev11")
          },
          latest_revision: 2,
          result: job
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderCalls += 1;
        // 遅延した古いrevision 10のsnapshot（現在は11）。巻き戻してはいけない。
        return jsonResponse({
          project: {
            id: "p1",
            title: "rev10-stale",
            work_name: "作品",
            revision: 10,
            manga_json: mangaWithPanel("rev10-stale", "p1/pages/stale-10.png")
          },
          latest_revision: 10,
          result: { panel_id: "p01_01", page_asset: "p1/pages/stale-10.png", warnings: [] }
        });
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "rev11", work_name: "作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        // 生成完了後の最新はrevision 11。
        return jsonResponse({
          id: "p1",
          title: "rev11",
          work_name: "作品",
          revision: generationStarted ? 11 : 0,
          manga_json: mangaWithPanel("rev11")
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("rev11"));
    await screen.findByDisplayValue("rev11");
    fireEvent.click(await screen.findByRole("button", { name: "選択コマを生成" }));

    // 古いrevision 10のrender応答が来ても、JSON・タイトルはrevision 11のまま。
    await waitFor(() => expect(renderCalls).toBe(1));
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    expect(textarea?.value).toContain("rev11");
    expect(textarea?.value).not.toContain("rev10-stale");
    expect(screen.getByLabelText("本のタイトル")).toHaveValue("rev11");
    // pageAssetsも古いsnapshotのasset(stale-10)で更新されない。
    const staleImg = Array.from(container.querySelectorAll("img")).find((img) =>
      img.getAttribute("src")?.includes("stale-10")
    );
    expect(staleImg).toBeUndefined();
  });

  it("render応答がlatest_revisionで再同期したら、旧result.page_assetでなく最新assetを表示する", async () => {
    let generationStarted = false;
    let renderDone = false;
    const job = {
      id: "job-1",
      project_id: "p1",
      panel_id: "p01_01",
      candidate_count: 1,
      status: "queued",
      progress: 0,
      message: "生成中",
      node: null,
      prompt_id: null,
      candidate_ids: [],
      created_at: "2026-01-01",
      updated_at: "2026-01-01"
    };
    class ImmediateWebSocket {
      onmessage: ((event: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() {
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({ ...job, status: "done", progress: 100, message: "完了" })
          });
        }, 0);
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", ImmediateWebSocket);
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 1,
            manga_json: mangaWithPanel("本")
          },
          latest_revision: 1,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/generation-jobs")) {
        generationStarted = true;
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 2,
            manga_json: mangaWithPanel("本")
          },
          latest_revision: 2,
          result: job
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderDone = true;
        // render snapshotはrevision 13・page_asset=stale-A。だがDBは既にrevision 14へ。
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 13,
            manga_json: mangaWithPanel("本", "p1/pages/stale-A.png")
          },
          latest_revision: 14,
          result: { panel_id: "p01_01", page_asset: "p1/pages/stale-A.png", warnings: [] }
        });
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "本", work_name: "作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        // 再同期reload(render後)では、最新revision 14・最新asset=fresh-Bを返す。
        return jsonResponse({
          id: "p1",
          title: "本",
          work_name: "作品",
          revision: renderDone ? 14 : generationStarted ? 12 : 0,
          manga_json: mangaWithPanel("本", renderDone ? "p1/pages/fresh-B.png" : null)
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("本"));
    await screen.findByDisplayValue("本");
    fireEvent.click(await screen.findByRole("button", { name: "選択コマを生成" }));

    await waitFor(() =>
      expect(
        screen.getByText("構成が更新されたためページ更新を中断しました。再実行してください。")
      ).toBeInTheDocument()
    );
    // 再同期後のプレビューは最新asset(fresh-B)で、古いrender結果(stale-A)では上書きしない。
    await waitFor(() => {
      const imgs = Array.from(container.querySelectorAll("img")).map((img) => img.getAttribute("src") ?? "");
      expect(imgs.some((src) => src.includes("fresh-B"))).toBe(true);
    });
    const staleImg = Array.from(container.querySelectorAll("img")).find((img) =>
      img.getAttribute("src")?.includes("stale-A")
    );
    expect(staleImg).toBeUndefined();
  });

  it("空ページ構成へ再同期したら以前のpage assetを消す", async () => {
    let renderDone = false;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 1,
            manga_json: mangaWithPanel("本", "p1/pages/old.png")
          },
          latest_revision: 1,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/render-page")) {
        renderDone = true;
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 2,
            manga_json: mangaWithPanel("本", "p1/pages/stale.png")
          },
          latest_revision: 3,
          result: { panel_id: "p01_01", page_asset: "p1/pages/stale.png", warnings: [] }
        });
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "本", work_name: "作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        return jsonResponse({
          id: "p1",
          title: "本",
          work_name: "作品",
          revision: renderDone ? 3 : 0,
          manga_json: renderDone
            ? { ...manga, title: "本", pages: [] }
            : mangaWithPanel("本", "p1/pages/old.png")
        });
      }
      return jsonResponse({});
    });

    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("本"));
    await screen.findByDisplayValue("本");
    expect(Array.from(container.querySelectorAll("img")).some((img) => img.src.includes("old.png"))).toBe(
      true
    );
    fireEvent.click(screen.getByTitle("全ページをレンダリング"));
    await waitFor(() =>
      expect(
        screen.getByText("構成が更新されたためレンダリングを中断しました。再実行してください。")
      ).toBeInTheDocument()
    );
    expect(Array.from(container.querySelectorAll("img")).some((img) => img.src.includes("old.png"))).toBe(
      false
    );
  });

  it("PageEditor保存後renderがtyped 409を返したら最新projectを採用する", async () => {
    const competitorManga = mangaWithPanel("競合で進んだ最新");
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      const status = statusResponses(url);
      if (status) return status;
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        return jsonResponse({
          project: {
            id: "p1",
            title: "本",
            work_name: "作品",
            revision: 1,
            manga_json: mangaWithPanel("本")
          },
          latest_revision: 1,
          result: {}
        });
      }
      if (init?.method === "POST" && url.includes("/render")) {
        // 保存後の /render が revision 競合。typed 409 を返す。
        return Promise.resolve(
          new Response(
            JSON.stringify({
              detail: "競合",
              code: "project_revision_conflict",
              expected_revision: 1,
              actual_revision: 7,
              project: {
                id: "p1",
                title: "競合で進んだ最新",
                work_name: "作品",
                revision: 7,
                manga_json: competitorManga
              }
            }),
            { status: 409, headers: { "Content-Type": "application/json" } }
          )
        );
      }
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "本", work_name: "作品", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        // 初期ロードはrevision 0の自分の状態。競合projectは409応答本体から採用する。
        return jsonResponse({
          id: "p1",
          title: "本",
          work_name: "作品",
          revision: 0,
          manga_json: mangaWithPanel("本")
        });
      }
      return jsonResponse({});
    });

    render(<App />);
    fireEvent.click(await screen.findByText("本"));
    await screen.findByDisplayValue("本");
    // 制作・編集タブ（既定）にページ編集キャンバスが統合されている。
    fireEvent.click(await screen.findByRole("button", { name: "保存（レイアウト確定）" }));

    // typed 409 が handleProjectMutationError に渡り、最新projectが採用される。
    await waitFor(() => expect(screen.getByLabelText("本のタイトル")).toHaveValue("競合で進んだ最新"));
  });

  it.each(["選択コマを生成", "ページを生成", "全ページを生成"])(
    "%s完了後のrender-pageには最新revisionを送る",
    async (buttonName) => {
      const mangaWithPanel = {
        ...manga,
        pages: [
          {
            page: 1,
            theme: "導入",
            layout_template: "one",
            reading_order: ["p01_01"],
            overlay_elements: [],
            panels: [
              {
                panel_id: "p01_01",
                bbox: [0.05, 0.05, 0.9, 0.9],
                shot: "wide",
                camera: "",
                location_id: "",
                characters: [],
                prompt: "",
                image_asset: null,
                image_candidates: [],
                selected_candidate_id: null,
                control_references: [],
                dialogue: [],
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
            ],
            render_status: "pending",
            rendered_at: null
          }
        ]
      } as MangaProject;
      const renderUrls: string[] = [];
      let generationStarted = false;
      const job = {
        id: "job-1",
        project_id: "p1",
        panel_id: "p01_01",
        candidate_count: 1,
        status: "queued",
        progress: 0,
        message: "生成中",
        node: null,
        prompt_id: null,
        candidate_ids: [],
        created_at: "2026-01-01",
        updated_at: "2026-01-01"
      };
      class ImmediateWebSocket {
        onmessage: ((event: { data: string }) => void) | null = null;
        onerror: (() => void) | null = null;
        onclose: (() => void) | null = null;
        constructor() {
          window.setTimeout(() => {
            this.onmessage?.({
              data: JSON.stringify({ ...job, status: "done", progress: 100, message: "完了" })
            });
          }, 0);
        }
        close() {}
      }
      vi.stubGlobal("WebSocket", ImmediateWebSocket);
      vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
        const url = String(input);
        if (init?.method === "PUT" && url.includes("/manga-json")) {
          return jsonResponse({
            project: {
              id: "p1",
              title: mangaWithPanel.title,
              work_name: mangaWithPanel.work_name,
              revision: 1,
              manga_json: mangaWithPanel
            },
            result: {}
          });
        }
        if (init?.method === "POST" && url.includes("/generation-jobs")) {
          generationStarted = true;
          const result = url.includes("/panels/") ? job : { jobs: [] };
          return jsonResponse({
            project: {
              id: "p1",
              title: mangaWithPanel.title,
              work_name: mangaWithPanel.work_name,
              revision: 2,
              manga_json: mangaWithPanel
            },
            result
          });
        }
        if (init?.method === "POST" && url.includes("/render-page")) {
          renderUrls.push(url);
          return jsonResponse({
            project: {
              id: "p1",
              title: mangaWithPanel.title,
              work_name: mangaWithPanel.work_name,
              revision: 5,
              manga_json: mangaWithPanel
            },
            result: { panel_id: "p01_01", page_asset: "p1/pages/page_001.png", warnings: [] }
          });
        }
        if (url === "/api/projects") {
          return jsonResponse([
            { id: "p1", title: "テスト本", work_name: "テスト作品", revision: 0, updated_at: "2026-01-01" }
          ]);
        }
        if (url === "/api/projects/p1") {
          return jsonResponse({
            id: "p1",
            title: mangaWithPanel.title,
            work_name: mangaWithPanel.work_name,
            revision: generationStarted ? 4 : 0,
            manga_json: mangaWithPanel
          });
        }
        if (url.endsWith("/production-status")) {
          return jsonResponse({
            project_id: "p1",
            status: "incomplete",
            adopted_panels: 0,
            total_panels: 1,
            rendered_pages: 0,
            total_pages: 1,
            pages: [],
            blockers: []
          });
        }
        if (url.endsWith("/generation-jobs")) return jsonResponse([]);
        if (url === "/api/comfyui/status") {
          return jsonResponse({
            backend: "stub",
            base_url: "",
            workflow_path: "",
            connected: false,
            workflow_exists: false,
            workflow_valid: false,
            missing_nodes: [],
            message: "stub"
          });
        }
        return jsonResponse({});
      });

      render(<App />);
      fireEvent.click(await screen.findByText("テスト本"));
      fireEvent.click(await screen.findByRole("button", { name: buttonName }));
      await waitFor(() => expect(renderUrls).toHaveLength(1));
      expect(renderUrls[0]).toContain("revision=4");
    }
  );
});

describe("品質ゲートの制作フロー", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const subjectWarning = (page: number, panelId: string) => ({
    level: "warning",
    code: "subject_too_small",
    message: `被写体が小さすぎます（${panelId}）`,
    page,
    panel_id: panelId,
    category: "image_quality",
    suggestion: "被写体を大きく配置してください",
    fixable: false
  });

  function mockQualityProject(): void {
    const twoPages = mangaWithTwoPages("品質テスト");
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/projects") {
        return jsonResponse([
          { id: "p1", title: "品質テスト", work_name: "", revision: 0, updated_at: "2026-01-01" }
        ]);
      }
      if (url === "/api/projects/p1") {
        return jsonResponse({
          id: "p1",
          title: "品質テスト",
          work_name: "",
          revision: 0,
          manga_json: twoPages
        });
      }
      if (url.endsWith("/production-status")) {
        return jsonResponse({
          project_id: "p1",
          status: "incomplete",
          adopted_panels: 0,
          total_panels: 2,
          rendered_pages: 0,
          total_pages: 2,
          pages: [
            {
              page: 1,
              status: "incomplete",
              adopted_panels: 0,
              total_panels: 1,
              rendered: false,
              blockers: [],
              quality_errors: [],
              quality_warnings: []
            },
            {
              page: 2,
              status: "incomplete",
              adopted_panels: 0,
              total_panels: 1,
              rendered: false,
              blockers: [],
              quality_errors: [],
              quality_warnings: [subjectWarning(2, "p02_01")]
            }
          ],
          blockers: [],
          quality_errors: [],
          quality_warnings: [subjectWarning(2, "p02_01")]
        });
      }
      if (url.endsWith("/generation-jobs")) return jsonResponse([]);
      if (url === "/api/comfyui/status") {
        return jsonResponse({
          backend: "stub",
          base_url: "",
          workflow_path: "",
          connected: false,
          workflow_exists: false,
          workflow_valid: false,
          missing_nodes: [],
          message: "stub"
        });
      }
      return jsonResponse({});
    });
  }

  it("品質警告を要修正一覧に表示し、クリックで対象ページ・コマへ移動する", async () => {
    mockQualityProject();
    render(<App />);
    fireEvent.click(await screen.findByText("品質テスト"));
    // 要修正コマ一覧に品質警告が出る。
    const issue = await screen.findByText("被写体が小さすぎます（p02_01）");
    expect(screen.getByText("再生成推奨")).toBeVisible();
    // 初期はページ1。ページ2のコマp02_01はまだ表示されていない。
    expect(screen.queryByText("p02_01")).toBeNull();
    // 警告クリックでページ2・コマp02_01へ移動する（コマ一覧と選択中コマに現れる）。
    fireEvent.click(issue);
    expect((await screen.findAllByText("p02_01")).length).toBeGreaterThan(0);
  });

  it("auto_candidatesトグルは既定でONになっている", async () => {
    mockQualityProject();
    render(<App />);
    fireEvent.click(await screen.findByText("品質テスト"));
    const toggle = await screen.findByLabelText("見せ場・複数人物は候補を自動で増やす");
    expect((toggle as HTMLInputElement).checked).toBe(true);
  });
});
