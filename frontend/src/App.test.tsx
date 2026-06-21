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
    });
  });
});
