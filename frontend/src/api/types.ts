import type { components } from "./schema";

export type ApiMangaProject = components["schemas"]["MangaProject"];
export type ApiPreflightResponse = components["schemas"]["PreflightResponse"];
export type ApiProjectDetail = components["schemas"]["ProjectDetail"];

export type ProjectMutationResponse<Project, Result> = {
  project: Project;
  // 応答整形時点のDB最新revision。project.revisionより大きければUIは最新へ再同期する。
  latest_revision: number;
  result: Result;
};
