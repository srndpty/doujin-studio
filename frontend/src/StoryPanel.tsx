import { useEffect, useMemo, useState } from "react";

type StageName = "brief" | "plot" | "pages" | "script";
type StageStatus = "empty" | "draft" | "approved";

type StageState = {
  status: StageStatus;
  data: Record<string, unknown> | null;
  knowledge_ids: string[];
  error: string | null;
  updated_at: string | null;
};

type StorySession = {
  id: string;
  project_id: string;
  work_name: string;
  target_pages: number;
  instruction: string;
  stages: Record<StageName, StageState>;
  created_at: string;
  updated_at: string;
};

type SessionSummary = {
  id: string;
  work_name: string;
  target_pages: number;
  instruction: string;
  updated_at: string;
};

type Revision = { id: string; project_id: string; label: string; created_at: string };

type LlmStatus = { provider: string; model: string; connected: boolean; message: string };
type LocalKnowledgeWork = {
  work_id: string;
  work_name: string;
  description: string;
  document_count: number;
};

const STAGES: { name: StageName; label: string; hint: string }[] = [
  { name: "brief", label: "企画", hint: "あらすじ・トーン・キャラの役割・原作準拠条件" },
  { name: "plot", label: "全体プロット", hint: "起承転結・主要ビート・キャラアーク" },
  { name: "pages", label: "ページ構成", hint: "各ページの目的・場面・登場人物・引き" },
  { name: "script", label: "コマ台本", hint: "shot・camera・location・visual prompt・台詞・効果音" }
];

const STATUS_LABEL: Record<StageStatus, string> = { empty: "未生成", draft: "未承認", approved: "承認済み" };

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export function StoryPanel({
  projectId,
  workName,
  onApplied,
  onBusyChange
}: {
  projectId: string;
  workName: string;
  onApplied: () => void;
  onBusyChange?: (busy: boolean, label: string) => void;
}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [session, setSession] = useState<StorySession | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [localWorks, setLocalWorks] = useState<LocalKnowledgeWork[]>([]);
  const [knowledgeWorkId, setKnowledgeWorkId] = useState("");
  const [targetPages, setTargetPages] = useState(4);
  const [instruction, setInstruction] = useState("");
  const [drafts, setDrafts] = useState<Record<StageName, string>>({
    brief: "",
    plot: "",
    pages: "",
    script: ""
  });
  const [message, setMessage] = useState("ストーリー生成セッションを作成してください");
  const [busy, setBusy] = useState(false);

  async function run(task: () => Promise<void>) {
    setBusy(true);
    onBusyChange?.(true, "ストーリー生成処理を実行中");
    try {
      await task();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "処理に失敗しました");
    } finally {
      setBusy(false);
      onBusyChange?.(false, "");
    }
  }

  async function refreshSessions() {
    setSessions(await getJson<SessionSummary[]>(`/api/projects/${projectId}/story-sessions`));
  }

  async function refreshRevisions() {
    setRevisions(await getJson<Revision[]>(`/api/projects/${projectId}/revisions`));
  }

  useEffect(() => {
    void getJson<LlmStatus>("/api/llm/status")
      .then(setLlm)
      .catch(() => setLlm(null));
    void getJson<LocalKnowledgeWork[]>("/api/knowledge/local-works")
      .then((works) => {
        setLocalWorks(works);
      })
      .catch(() => setLocalWorks([]));
  }, []);

  useEffect(() => {
    const matched = localWorks.find((work) => work.work_name === workName);
    setKnowledgeWorkId(matched?.work_id ?? localWorks[0]?.work_id ?? "");
  }, [workName, localWorks]);

  useEffect(() => {
    setSession(null);
    void refreshSessions();
    void refreshRevisions();
  }, [projectId]);

  function syncDrafts(next: StorySession) {
    setSession(next);
    setDrafts({
      brief: JSON.stringify(next.stages.brief.data ?? {}, null, 2),
      plot: JSON.stringify(next.stages.plot.data ?? {}, null, 2),
      pages: JSON.stringify(next.stages.pages.data ?? {}, null, 2),
      script: JSON.stringify(next.stages.script.data ?? {}, null, 2)
    });
  }

  async function createSession() {
    await run(async () => {
      const created = await sendJson<StorySession>(`/api/projects/${projectId}/story-sessions`, "POST", {
        work_name: selectedLocalWork?.work_name ?? workName,
        knowledge_work_id: knowledgeWorkId,
        target_pages: targetPages,
        instruction
      });
      syncDrafts(created);
      setMessage(
        knowledgeWorkId
          ? `「${created.work_name}」のローカル知識を同期してセッションを作成しました`
          : "セッションを作成しました。企画から生成してください"
      );
      await refreshSessions();
      if (knowledgeWorkId) onApplied();
    });
  }

  async function loadSession(id: string) {
    await run(async () => {
      syncDrafts(await getJson<StorySession>(`/api/story-sessions/${id}`));
      setMessage("セッションを読み込みました");
    });
  }

  async function generateStage(stage: StageName) {
    if (!session) return;
    await run(async () => {
      setMessage(`${stage}を生成中…`);
      const next = await sendJson<StorySession>(
        `/api/story-sessions/${session.id}/stages/${stage}/generate`,
        "POST",
        { instruction: "" }
      );
      syncDrafts(next);
      setMessage(
        next.stages[stage].error ? `生成エラー: ${next.stages[stage].error}` : `${stage}を生成しました`
      );
    });
  }

  async function saveStage(stage: StageName) {
    if (!session) return;
    await run(async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(drafts[stage]);
      } catch {
        throw new Error("JSONの形式が不正です");
      }
      const next = await sendJson<StorySession>(`/api/story-sessions/${session.id}/stages/${stage}`, "PUT", {
        data: parsed
      });
      syncDrafts(next);
      setMessage(`${stage}を保存しました（下流段階は未承認へ戻ります）`);
    });
  }

  async function approveStage(stage: StageName) {
    if (!session) return;
    await run(async () => {
      const next = await sendJson<StorySession>(
        `/api/story-sessions/${session.id}/stages/${stage}/approve`,
        "POST"
      );
      syncDrafts(next);
      setMessage(`${stage}を承認しました`);
    });
  }

  async function applySession() {
    if (!session) return;
    await run(async () => {
      await sendJson(`/api/story-sessions/${session.id}/apply`, "POST");
      setMessage("プロジェクトへ適用しました。適用前の状態はリビジョンに保存されています");
      await refreshRevisions();
      onApplied();
    });
  }

  async function restoreRevision(id: string) {
    await run(async () => {
      await sendJson(`/api/projects/${projectId}/revisions/${id}/restore`, "POST");
      setMessage("リビジョンを復元しました");
      await refreshRevisions();
      onApplied();
    });
  }

  const scriptApproved = session?.stages.script.status === "approved";
  const selectedLocalWork = localWorks.find((work) => work.work_id === knowledgeWorkId);
  const canGenerate = useMemo(() => {
    const result: Record<StageName, boolean> = { brief: false, plot: false, pages: false, script: false };
    if (!session) return result;
    result.brief = true;
    result.plot = session.stages.brief.status === "approved";
    result.pages = session.stages.plot.status === "approved";
    result.script = session.stages.pages.status === "approved";
    return result;
  }, [session]);

  return (
    <section className="story-panel">
      <div className="panel-message">{message}</div>
      {llm && (
        <div className={`llm-band ${llm.connected ? "ok" : "ng"}`}>
          <strong>
            LLM: {llm.provider}
            {llm.model ? ` / ${llm.model}` : ""}
          </strong>
          <span>{llm.message}</span>
        </div>
      )}

      <div className="story-session-bar">
        <div className="settings-grid">
          <label>
            作品知識
            <select value={knowledgeWorkId} onChange={(event) => setKnowledgeWorkId(event.target.value)}>
              <option value="">ローカル知識を使用しない</option>
              {localWorks.map((work) => (
                <option key={work.work_id} value={work.work_id}>
                  {work.work_name}（{work.document_count}ファイル）
                </option>
              ))}
            </select>
            {selectedLocalWork?.description && <small>{selectedLocalWork.description}</small>}
          </label>
          <label>
            ページ数
            <select value={targetPages} onChange={(event) => setTargetPages(Number(event.target.value))}>
              <option value={4}>4ページ</option>
              <option value={8}>8ページ</option>
              <option value={16}>16ページ</option>
            </select>
          </label>
          <label>
            全体方針
            <input
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="作りたい話の方向"
            />
          </label>
        </div>
        <button onClick={createSession} disabled={busy}>
          新規セッション
        </button>
        {sessions.length > 0 && (
          <select
            value={session?.id ?? ""}
            onChange={(event) => event.target.value && void loadSession(event.target.value)}
          >
            <option value="">既存セッションを選択</option>
            {sessions.map((item) => (
              <option key={item.id} value={item.id}>
                {item.target_pages}p / {item.instruction || "方針なし"} /{" "}
                {new Date(item.updated_at).toLocaleString()}
              </option>
            ))}
          </select>
        )}
      </div>

      {session && (
        <div className="stage-list">
          {STAGES.map(({ name, label, hint }) => {
            const stage = session.stages[name];
            return (
              <article key={name} className={`stage-card ${stage.status}`}>
                <header>
                  <div>
                    <strong>{label}</strong>
                    <small>{hint}</small>
                  </div>
                  <span className={`stage-status ${stage.status}`}>{STATUS_LABEL[stage.status]}</span>
                </header>
                {stage.error && <p className="stage-error">{stage.error}</p>}
                {stage.knowledge_ids.length > 0 && (
                  <small className="stage-knowledge">参照知識 {stage.knowledge_ids.length}件</small>
                )}
                <StageSummary name={name} data={stage.data} />
                <details className="stage-json">
                  <summary>JSONを編集</summary>
                  <textarea
                    value={drafts[name]}
                    onChange={(event) => setDrafts((current) => ({ ...current, [name]: event.target.value }))}
                    spellCheck={false}
                    rows={10}
                  />
                  <button onClick={() => void saveStage(name)} disabled={busy}>
                    保存
                  </button>
                </details>
                <div className="actions">
                  <button onClick={() => void generateStage(name)} disabled={busy || !canGenerate[name]}>
                    {stage.status === "empty" ? "生成" : "再生成"}
                  </button>
                  <button
                    onClick={() => void approveStage(name)}
                    disabled={busy || !stage.data || stage.status === "approved"}
                  >
                    承認
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {session && (
        <div className="story-apply">
          <button className="primary" onClick={applySession} disabled={busy || !scriptApproved}>
            プロジェクトへ適用
          </button>
          {!scriptApproved && <small className="hint">台本まで承認すると適用できます</small>}
        </div>
      )}

      <div className="revision-list">
        <h3>リビジョン（{revisions.length}）</h3>
        {revisions.length === 0 && <small className="hint">適用や復元を行うとリビジョンが残ります</small>}
        {revisions.map((revision) => (
          <article key={revision.id} className="revision-card">
            <div>
              <strong>{revision.label}</strong>
              <small>{new Date(revision.created_at).toLocaleString()}</small>
            </div>
            <button onClick={() => void restoreRevision(revision.id)} disabled={busy}>
              復元
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}

function StageSummary({ name, data }: { name: StageName; data: Record<string, unknown> | null }) {
  if (!data) return <small className="hint">未生成です</small>;
  if (name === "brief") {
    return (
      <div className="stage-summary">
        <p>{String(data.synopsis ?? "")}</p>
        <small>トーン: {String(data.tone ?? "")}</small>
        <small>
          キャラ: {(data.characters as { name: string }[] | undefined)?.map((c) => c.name).join("、")}
        </small>
      </div>
    );
  }
  if (name === "plot") {
    return (
      <div className="stage-summary">
        <small>起: {String(data.ki ?? "")}</small>
        <small>承: {String(data.sho ?? "")}</small>
        <small>転: {String(data.ten ?? "")}</small>
        <small>結: {String(data.ketsu ?? "")}</small>
      </div>
    );
  }
  if (name === "pages") {
    const pages = (data.pages as { page: number; purpose: string }[] | undefined) ?? [];
    return (
      <div className="stage-summary">
        {pages.map((page) => (
          <small key={page.page}>
            {page.page}p: {page.purpose}
          </small>
        ))}
      </div>
    );
  }
  const pages = (data.pages as { page: number; panels: unknown[] }[] | undefined) ?? [];
  return (
    <div className="stage-summary">
      {pages.map((page) => (
        <small key={page.page}>
          {page.page}p: {page.panels.length}コマ
        </small>
      ))}
    </div>
  );
}
