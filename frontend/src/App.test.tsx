import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, type MangaProject } from "./App";

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

describe("AppのManga JSON保存", () => {
  afterEach(() => vi.restoreAllMocks());

  it("直接編集したjsonTextをツールバー保存で送信する", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/manga-json")) {
        const body = JSON.parse(String(init.body)) as MangaProject;
        return jsonResponse({
          id: "p1",
          title: body.title,
          work_name: body.work_name,
          revision: 1,
          manga_json: body
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
        return Promise.resolve(new Response("conflict", { status: 409 }));
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
  });

  it("プロジェクト切替後は古いCBZ応答を現在の編集状態へ反映しない", async () => {
    const p2Manga = { ...manga, title: "プロジェクト2" };
    let resolveCbz!: (response: Response) => void;
    const delayedCbz = new Promise<Response>((resolve) => {
      resolveCbz = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "POST" && url === "/api/projects/p1/export/cbz") return delayedCbz;
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
    fireEvent.click(screen.getByTitle("CBZを書き出す"));
    fireEvent.click(screen.getByText("プロジェクト2"));
    await screen.findByDisplayValue("プロジェクト2");

    resolveCbz(
      new Response(
        JSON.stringify({
          cbz_asset: "p1/old.cbz",
          absolute_path: "old.cbz",
          revision: 9,
          manga_json: { ...manga, title: "遅延したプロジェクト1" },
          warnings: []
        }),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    await waitFor(() => expect(screen.getByLabelText("本のタイトル")).toHaveValue("プロジェクト2"));
    const textarea = container.querySelector<HTMLTextAreaElement>(".json-pane textarea");
    expect(textarea?.value).toContain("プロジェクト2");
    expect(textarea?.value).not.toContain("遅延したプロジェクト1");
  });
});
