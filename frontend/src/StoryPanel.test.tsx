import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StoryPanel } from "./StoryPanel";

function jsonResponse(value: unknown): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } })
  );
}

function props(projectId: string) {
  return {
    projectId,
    revision: 0,
    workName: "作品",
    onProjectMutation: vi.fn(),
    onProjectMutationError: vi.fn().mockResolvedValue(false)
  };
}

const emptyStage = {
  status: "empty",
  data: null,
  knowledge_ids: [],
  error: null,
  warnings: [],
  updated_at: null
} as const;

function storySession(overrides: Partial<Record<"brief" | "plot" | "pages" | "script", unknown>> = {}) {
  return {
    id: "s1",
    project_id: "p1",
    work_name: "作品",
    target_pages: 4,
    instruction: "方針",
    stages: {
      brief: { ...emptyStage, ...(overrides.brief ?? {}) },
      plot: { ...emptyStage, ...(overrides.plot ?? {}) },
      pages: { ...emptyStage, ...(overrides.pages ?? {}) },
      script: { ...emptyStage, ...(overrides.script ?? {}) }
    },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z"
  };
}

describe("StoryPanel", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("strict外部LLM障害をstub退避と誤表示しない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/llm/status") {
          return jsonResponse({
            provider: "openai_compatible",
            model: "missing",
            connected: false,
            message: "指定モデルが未ロードです"
          });
        }
        return jsonResponse([]);
      })
    );

    render(<StoryPanel {...props("p1")} />);

    expect(
      await screen.findByText("警告: 外部LLMを利用できないため、ストーリー生成は実行できません。")
    ).toBeInTheDocument();
    expect(screen.queryByText(/スタブ生成では/)).not.toBeInTheDocument();
  });

  it("切替前projectの遅延session一覧を反映しない", async () => {
    let resolveOld!: (response: Response) => void;
    const oldSessions = new Promise<Response>((resolve) => {
      resolveOld = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/projects/p1/story-sessions") return oldSessions;
        if (url === "/api/llm/status") {
          return jsonResponse({ provider: "stub", model: "stub", connected: true, message: "stub" });
        }
        return jsonResponse([]);
      })
    );

    const view = render(<StoryPanel {...props("p1")} />);
    view.rerender(<StoryPanel {...props("p2")} />);
    resolveOld(
      new Response(
        JSON.stringify([
          {
            id: "old-session",
            project_id: "p1",
            work_name: "旧作品",
            target_pages: 4,
            instruction: "旧プロジェクトの方針",
            updated_at: "2026-01-01T00:00:00Z"
          }
        ]),
        { headers: { "Content-Type": "application/json" } }
      )
    );

    await waitFor(() => {
      expect(screen.queryByRole("option", { name: /旧プロジェクトの方針/ })).not.toBeInTheDocument();
    });
  });

  it("台本段階の編集チェック警告を一覧表示する", async () => {
    const session = storySession({
      script: {
        status: "draft",
        data: { pages: [] },
        knowledge_ids: [],
        error: null,
        warnings: ["1ページ コマ1: 台詞「ダメ！」は擬音の可能性があります（擬音欄への移動を検討）"],
        updated_at: "2026-01-01T00:00:00Z"
      }
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/llm/status") {
          return jsonResponse({ provider: "stub", model: "stub", connected: true, message: "stub" });
        }
        if (url === "/api/projects/p1/story-sessions") {
          return jsonResponse([
            {
              id: "s1",
              project_id: "p1",
              work_name: "作品",
              target_pages: 4,
              instruction: "方針",
              updated_at: "2026-01-01T00:00:00Z"
            }
          ]);
        }
        if (url === "/api/story-sessions/s1") return jsonResponse(session);
        return jsonResponse([]);
      })
    );

    render(<StoryPanel {...props("p1")} />);

    expect(await screen.findByText(/台詞「ダメ！」は擬音の可能性があります/)).toBeInTheDocument();
  });

  it("企画からコマ台本まで一括生成できる", async () => {
    const calls: string[] = [];
    const initial = storySession();
    const generated = {
      brief: { synopsis: "導入", tone: "明るい", characters: [] },
      plot: { ki: "起", sho: "承", ten: "転", ketsu: "結", beats: [], character_arcs: [] },
      pages: { pages: [{ page: 1, purpose: "始める" }] },
      script: { pages: [{ page: 1, panels: [] }] }
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/llm/status") {
          return jsonResponse({ provider: "stub", model: "stub", connected: true, message: "stub" });
        }
        if (url === "/api/projects/p1/story-sessions") {
          return jsonResponse([
            {
              id: "s1",
              project_id: "p1",
              work_name: "作品",
              target_pages: 4,
              instruction: "方針",
              updated_at: "2026-01-01T00:00:00Z"
            }
          ]);
        }
        if (url === "/api/story-sessions/s1") return jsonResponse(initial);
        if (url === "/api/story-sessions/s1/generation-progress") {
          return jsonResponse({ chars: 0, tail: "" });
        }
        const matched = url.match(/^\/api\/story-sessions\/s1\/stages\/(.+)\/generate$/);
        if (method === "POST" && matched) {
          const stage = matched[1] as keyof typeof generated;
          calls.push(stage);
          return jsonResponse(
            storySession({
              brief: calls.includes("brief") ? { status: "draft", data: generated.brief } : {},
              plot: calls.includes("plot") ? { status: "draft", data: generated.plot } : {},
              pages: calls.includes("pages") ? { status: "draft", data: generated.pages } : {},
              script: calls.includes("script") ? { status: "draft", data: generated.script } : {}
            })
          );
        }
        return jsonResponse([]);
      })
    );

    render(<StoryPanel {...props("p1")} />);

    fireEvent.click(await screen.findByRole("button", { name: "企画→コマ台本を一括生成" }));

    await waitFor(() => {
      expect(calls).toEqual(["brief", "plot", "pages", "script"]);
    });
    expect(await screen.findByText("企画からコマ台本まで一括生成しました")).toBeInTheDocument();
  });
});
