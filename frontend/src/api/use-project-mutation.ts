import { useCallback } from "react";
import { ApiError } from "./client";
import type { ProjectMutationResponse } from "./types";

type ProjectWithRevision = { id: string; revision: number };

type Options<Project extends ProjectWithRevision> = {
  // 反映できた(=巻き戻しでない)ときtrue。falseなら後続のonConflict等は呼ばない。
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
 * applyProjectがfalse(巻き戻し回避で反映しなかった)を返した場合は派生処理を行わない。
 */
export function useProjectMutation<Project extends ProjectWithRevision>({
  applyProject,
  onConflict,
  onStale,
  onPartialSuccess
}: Options<Project>) {
  const resyncIfStale = useCallback(
    (project: Project, latestRevision: number | undefined) => {
      if (latestRevision !== undefined && latestRevision > project.revision) {
        void onStale(project.id);
      }
    },
    [onStale]
  );

  const applyMutationResponse = useCallback(
    <Result>(response: ProjectMutationResponse<Project, Result>): Result => {
      // 遅延到着で巻き戻さずに反映できた場合のみ、より新しいDB状態があれば再同期する。
      if (applyProject(response.project)) {
        resyncIfStale(response.project, response.latest_revision);
      }
      return response.result;
    },
    [applyProject, resyncIfStale]
  );

  const handleProjectMutationError = useCallback(
    async (error: unknown): Promise<boolean> => {
      if (!(error instanceof ApiError) || error.status !== 409 || !error.body?.project) {
        return false;
      }
      if (error.body.code === "project_revision_conflict") {
        const project = error.body.project as Project;
        // 巻き戻しになる場合は派生状態を旧snapshotで作り直さない。
        if (applyProject(project)) await onConflict(project);
        return true;
      }
      if (error.body.code === "project_mutation_partially_applied") {
        // 前段(候補採用など)は適用済み。projectは応答時点の最新state。反映できたときだけ
        // 部分成功通知と、更に新しいDB状態への再同期を行う。
        const project = error.body.project as Project;
        if (applyProject(project)) {
          resyncIfStale(project, error.body.latest_revision);
          await onPartialSuccess?.(
            project,
            String(error.body.completed_operation ?? ""),
            String(error.body.failed_operation ?? "")
          );
        }
        return true;
      }
      return false;
    },
    [applyProject, onConflict, onPartialSuccess, resyncIfStale]
  );

  return { applyMutationResponse, handleProjectMutationError };
}
