import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { useProjectMutation } from "./use-project-mutation";

type Project = { id: string; revision: number; title: string };

function setup(overrides: Partial<Parameters<typeof useProjectMutation<Project>>[0]> = {}) {
  const applyProject = vi.fn<(project: Project) => boolean>(() => true);
  const onConflict = vi.fn();
  const onStale = vi.fn();
  const onPartialSuccess = vi.fn();
  const hook = renderHook(() =>
    useProjectMutation<Project>({ applyProject, onConflict, onStale, onPartialSuccess, ...overrides })
  );
  return { hook, applyProject, onConflict, onStale, onPartialSuccess };
}

describe("useProjectMutation", () => {
  it("latest_revisionがproject.revisionより新しいときは最新へ再同期する", () => {
    const { hook, applyProject, onStale } = setup();
    act(() => {
      hook.result.current.applyMutationResponse({
        project: { id: "p1", revision: 6, title: "操作時点" },
        latest_revision: 8,
        result: { ok: true }
      });
    });
    expect(applyProject).toHaveBeenCalledWith({ id: "p1", revision: 6, title: "操作時点" });
    // snapshotより新しいDB状態があるので再同期する。
    expect(onStale).toHaveBeenCalledWith("p1");
  });

  it("latest_revisionがproject.revisionと等しいときは再同期しない", () => {
    const { hook, onStale } = setup();
    act(() => {
      hook.result.current.applyMutationResponse({
        project: { id: "p1", revision: 8, title: "最新" },
        latest_revision: 8,
        result: {}
      });
    });
    expect(onStale).not.toHaveBeenCalled();
  });

  it("巻き戻しで反映されなかった応答では再同期しない", () => {
    const { hook, onStale } = setup({ applyProject: vi.fn(() => false) });
    act(() => {
      hook.result.current.applyMutationResponse({
        project: { id: "p1", revision: 3, title: "古い" },
        latest_revision: 9,
        result: {}
      });
    });
    expect(onStale).not.toHaveBeenCalled();
  });

  it("部分成功(partially_applied)は前段stateを採用し専用通知する", async () => {
    const { hook, applyProject, onPartialSuccess, onConflict } = setup();
    const project = { id: "p1", revision: 7, title: "採用済み" };
    const error = new ApiError(409, "partial", {
      code: "project_mutation_partially_applied",
      completed_operation: "candidate_selection",
      failed_operation: "render_page",
      project
    });
    let handled = false;
    await act(async () => {
      handled = await hook.result.current.handleProjectMutationError(error);
    });
    expect(handled).toBe(true);
    expect(applyProject).toHaveBeenCalledWith(project);
    expect(onPartialSuccess).toHaveBeenCalledWith(project, "candidate_selection", "render_page");
    // 通常のrevision競合扱いにはしない。
    expect(onConflict).not.toHaveBeenCalled();
  });

  it("revision競合はonConflictで最新を採用する", async () => {
    const { hook, applyProject, onConflict, onPartialSuccess } = setup();
    const project = { id: "p1", revision: 9, title: "最新" };
    const error = new ApiError(409, "conflict", {
      code: "project_revision_conflict",
      expected_revision: 5,
      actual_revision: 9,
      project
    });
    let handled = false;
    await act(async () => {
      handled = await hook.result.current.handleProjectMutationError(error);
    });
    expect(handled).toBe(true);
    expect(applyProject).toHaveBeenCalledWith(project);
    expect(onConflict).toHaveBeenCalledWith(project);
    expect(onPartialSuccess).not.toHaveBeenCalled();
  });
});
