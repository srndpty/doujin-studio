import { useCallback } from "react";
import { ApiError } from "./client";

export type ProjectMutationResponse<Project, Result> = {
  project: Project;
  result: Result;
};

type Options<Project> = {
  applyProject: (project: Project) => boolean;
  onConflict: (project: Project) => void | Promise<void>;
};

/**
 * project mutation成功時の全体反映と、revision競合時の安全な最新採用を集約する。
 * 通常の409は対象外とし、サーバーが明示したrevision競合だけを処理する。
 */
export function useProjectMutation<Project>({ applyProject, onConflict }: Options<Project>) {
  const applyMutationResponse = useCallback(
    <Result>(response: ProjectMutationResponse<Project, Result>): Result => {
      applyProject(response.project);
      return response.result;
    },
    [applyProject]
  );

  const handleProjectMutationError = useCallback(
    async (error: unknown): Promise<boolean> => {
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.body?.code === "project_revision_conflict" &&
        error.body.project
      ) {
        const project = error.body.project as Project;
        applyProject(project);
        await onConflict(project);
        return true;
      }
      return false;
    },
    [applyProject, onConflict]
  );

  return { applyMutationResponse, handleProjectMutationError };
}
