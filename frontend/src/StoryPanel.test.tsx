import { render, screen, waitFor } from "@testing-library/react";
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
});
