export type ApiErrorBody = {
  detail?: string;
  code?: string;
  expected_revision?: number;
  actual_revision?: number;
  // 部分成功(project_mutation_partially_applied)で確定/失敗した操作名と前段snapshot。
  completed_operation?: string;
  failed_operation?: string;
  completed_project?: unknown;
  // revision競合・部分成功応答が同梱する応答整形時点のDB最新revision。
  latest_revision?: number;
  project?: unknown;
};

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody | null;

  constructor(status: number, message: string, body: ApiErrorBody | null = null) {
    super(message);
    this.status = status;
    this.body = body;
    this.name = "ApiError";
  }
}

async function parseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as ApiErrorBody;
    return new ApiError(response.status, body.detail ?? text, body);
  } catch {
    return new ApiError(response.status, text || `HTTP ${response.status}`);
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

export const api = {
  async get<T>(path: string): Promise<T> {
    return readJson<T>(await fetch(path));
  },

  async post<T>(path: string, body?: unknown): Promise<T> {
    return readJson<T>(
      await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body)
      })
    );
  },

  async put<T>(path: string, body: unknown): Promise<T> {
    return readJson<T>(
      await fetch(path, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      })
    );
  },

  async delete<T>(path: string): Promise<T> {
    return readJson<T>(await fetch(path, { method: "DELETE" }));
  },

  async postBinary<T>(path: string, file: File): Promise<T> {
    return readJson<T>(
      await fetch(path, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file
      })
    );
  }
};

export function withRevision(path: string, revision: number): string {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}revision=${revision}`;
}
