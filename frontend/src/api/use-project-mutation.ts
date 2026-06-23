import { useCallback } from "react";
import { ApiError } from "./client";

export type ProjectMutationResponse<Project, Result> = {
  project: Project;
  // 応答整形時点のDB最新revision。project.revisionより大きければUIは最新へ再同期する。
  latest_revision?: number;
  result: Result;
};

type ProjectWithRevision = { id: string; revision: number };

type Options<Project extends ProjectWithRevision> = {
  applyProject: (project: Project) => boolean;
  onConflict: (project: Project) => void | Promise<void>;
  // project.revisionがlatest_revisionより古い成功応答を反映した後、最新へ再同期する。
  onStale: (projectId: string) => void | Promise<void>;
  // 複合操作の前段だけ確定した部分成功（採用済み等）をユーザーへ通知する。
  onPartialSuccess?: (project: Project, completed: string, failed: string) => void | Promise<void>;
};

/**
 * project mutation成功時の全体反映と、revision競合時の安全な最新採用を集約する。
 * 通常の409は対象外とし、サーバーが明示したrevision競合・部分成功だけを処理する。
 */
export function useProjectMutation<Project extends ProjectWithRevision>({
  applyProject,
  onConflict,
  onStale,
  onPartialSuccess
}: Options<Project>) {
  const applyMutationResponse = useCallback(
    <Result>(response: ProjectMutationResponse<Project, Result>): Result => {
      const applied = applyProject(response.project);
      // 遅延到着で巻き戻さずに反映できた場合のみ、より新しいDB状態があれば再同期する。
      if (
        applied &&
        response.latest_revision !== undefined &&
        response.latest_revision > response.project.revision
      ) {
        void onStale(response.project.id);
      }
      return response.result;
    },
    [applyProject, onStale]
  );

  const handleProjectMutationError = useCallback(
    async (error: unknown): Promise<boolean> => {
      if (!(error instanceof ApiError) || error.status !== 409 || !error.body?.project) {
        return false;
      }
      if (error.body.code === "project_revision_conflict") {
        const project = error.body.project as Project;
        applyProject(project);
        await onConflict(project);
        return true;
      }
      if (error.body.code === "project_mutation_partially_applied") {
        // 前段(候補採用など)は適用済み。最新stateを採用しつつ部分成功として通知する。
        const project = error.body.project as Project;
        applyProject(project);
        await onPartialSuccess?.(
          project,
          String(error.body.completed_operation ?? ""),
          String(error.body.failed_operation ?? "")
        );
        return true;
      }
      return false;
    },
    [applyProject, onConflict, onPartialSuccess]
  );

  return { applyMutationResponse, handleProjectMutationError };
}
