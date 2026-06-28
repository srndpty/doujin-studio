import { useEffect, useMemo, useRef, useState } from "react";
import { api, withRevision } from "./api/client";
import type { ProjectMutationResponse } from "./api/use-project-mutation";

type StageName = "brief" | "plot" | "pages" | "script";
type StageStatus = "empty" | "draft" | "approved";

type StageState = {
  status: StageStatus;
  data: Record<string, unknown> | null;
  knowledge_ids: string[];
  error: string | null;
  warnings?: string[];
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

const STATUS_LABEL: Record<StageStatus, string> = {
  empty: "未生成",
  draft: "生成済み",
  approved: "生成済み"
};

export function StoryPanel<ProjectType>({
  projectId,
  revision,
  workName,
  onProjectMutation,
  onProjectMutationError,
  onBusyChange
}: {
  projectId: string;
  revision: number;
  workName: string;
  onProjectMutation: (response: ProjectMutationResponse<ProjectType, unknown>) => void | Promise<void>;
  onProjectMutationError: (error: unknown) => boolean | Promise<boolean>;
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
  const [genProgress, setGenProgress] = useState<{ chars: number; tail: string; seconds: number } | null>(
    null
  );
  const activeProjectIdRef = useRef(projectId);
  activeProjectIdRef.current = projectId;

  async function run(task: () => Promise<void>) {
    setBusy(true);
    onBusyChange?.(true, "ストーリー生成処理を実行中");
    try {
      await task();
    } catch (error) {
      if (await onProjectMutationError(error)) {
        setMessage("他の操作で更新されたため、最新のプロジェクトを採用しました");
      } else {
        setMessage(error instanceof Error ? error.message : "処理に失敗しました");
      }
    } finally {
      setBusy(false);
      onBusyChange?.(false, "");
    }
  }

  async function fetchSessions(targetProjectId: string) {
    return api.get<SessionSummary[]>(`/api/projects/${targetProjectId}/story-sessions`);
  }

  async function refreshLlm() {
    const status = await api.get<LlmStatus>("/api/llm/status");
    setLlm(status);
    return status;
  }

  async function fetchRevisions(targetProjectId: string) {
    return api.get<Revision[]>(`/api/projects/${targetProjectId}/revisions`);
  }

  useEffect(() => {
    void refreshLlm().catch(() => setLlm(null));
    void api
      .get<LocalKnowledgeWork[]>("/api/knowledge/local-works")
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
    setSessions([]);
    setRevisions([]);
    let cancelled = false;
    const requestedProjectId = projectId;
    void fetchSessions(requestedProjectId).then(async (items) => {
      if (cancelled || activeProjectIdRef.current !== requestedProjectId) return;
      setSessions(items);
      if (items.length === 0) return;
      const latest = await api.get<StorySession>(`/api/story-sessions/${items[0].id}`);
      if (
        !cancelled &&
        activeProjectIdRef.current === requestedProjectId &&
        latest.project_id === requestedProjectId
      ) {
        syncDrafts(latest);
      }
    });
    void fetchRevisions(requestedProjectId).then((items) => {
      if (!cancelled && activeProjectIdRef.current === requestedProjectId) setRevisions(items);
    });
    return () => {
      cancelled = true;
    };
    // プロジェクトIDが変わったときだけ再取得する。
  }, [projectId]);

  function syncDrafts(next: StorySession) {
    setSession(next);
    setInstruction(next.instruction);
    setTargetPages(next.target_pages);
    setDrafts({
      brief: JSON.stringify(next.stages.brief.data ?? {}, null, 2),
      plot: JSON.stringify(next.stages.plot.data ?? {}, null, 2),
      pages: JSON.stringify(next.stages.pages.data ?? {}, null, 2),
      script: JSON.stringify(next.stages.script.data ?? {}, null, 2)
    });
  }

  async function createSession() {
    await run(async () => {
      const response = await api.post<ProjectMutationResponse<ProjectType, StorySession>>(
        withRevision(`/api/projects/${projectId}/story-sessions`, revision),
        {
          work_name: selectedLocalWork?.work_name ?? workName,
          knowledge_work_id: knowledgeWorkId,
          target_pages: targetPages,
          instruction
        }
      );
      await onProjectMutation(response);
      syncDrafts(response.result);
      setMessage(
        knowledgeWorkId
          ? `「${response.result.work_name}」のローカル知識を同期してセッションを作成しました`
          : "セッションを作成しました。企画から生成してください"
      );
      const items = await fetchSessions(projectId);
      if (activeProjectIdRef.current === projectId) setSessions(items);
    });
  }

  async function loadSession(id: string) {
    await run(async () => {
      const next = await api.get<StorySession>(`/api/story-sessions/${id}`);
      if (next.project_id !== activeProjectIdRef.current) return;
      syncDrafts(next);
      setMessage("セッションを読み込みました");
    });
  }

  async function generateStage(stage: StageName) {
    if (!session) return;
    await run(async () => {
      await refreshLlm();
      const next = await generateStageRequest(session.id, stage);
      syncDrafts(next);
      setMessage(
        next.stages[stage].error ? `生成エラー: ${next.stages[stage].error}` : `${stage}を生成しました`
      );
    });
  }

  async function generateAllStages() {
    if (!session) return;
    await run(async () => {
      await refreshLlm();
      let next = session;
      for (const { name, label } of STAGES) {
        setMessage(`${label}を生成中…`);
        next = await generateStageRequest(next.id, name, label);
        syncDrafts(next);
        if (next.stages[name].error) {
          setMessage(`一括生成を停止しました: ${label}の生成エラー: ${next.stages[name].error}`);
          return;
        }
      }
      setMessage("企画からコマ台本まで一括生成しました");
    });
  }

  async function generateStageRequest(sessionId: string, stage: StageName, label?: string) {
    const stageLabel = label ?? STAGES.find((item) => item.name === stage)?.label ?? stage;
    setMessage(`${stageLabel}を生成中…`);
    const startedAt = Date.now();
    setGenProgress({ chars: 0, tail: "", seconds: 0 });
    // 生成POSTがブロックする間、別リクエストで進捗をポーリングして「止まっていない」
    // ことと受信状況（文字数・出力末尾）を表示する。
    const timer = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      void api
        .get<{ chars?: number; tail?: string }>(`/api/story-sessions/${sessionId}/generation-progress`)
        .then((p) => setGenProgress({ chars: p?.chars ?? 0, tail: p?.tail ?? "", seconds }))
        .catch(() =>
          setGenProgress((current) => (current ? { ...current, seconds } : { chars: 0, tail: "", seconds }))
        );
    }, 800);
    try {
      return await api.post<StorySession>(`/api/story-sessions/${sessionId}/stages/${stage}/generate`, {
        instruction: ""
      });
    } finally {
      window.clearInterval(timer);
      setGenProgress(null);
    }
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
      const next = await api.put<StorySession>(`/api/story-sessions/${session.id}/stages/${stage}`, {
        data: parsed
      });
      syncDrafts(next);
      setMessage(`${stage}を保存しました（下流段階は未承認へ戻ります）`);
    });
  }

  async function applySession() {
    if (!session) return;
    await run(async () => {
      const response = await api.post<ProjectMutationResponse<ProjectType, unknown>>(
        withRevision(`/api/story-sessions/${session.id}/apply`, revision)
      );
      await onProjectMutation(response);
      setMessage("プロジェクトへ適用しました。適用前の状態はリビジョンに保存されています");
      const items = await fetchRevisions(projectId);
      if (activeProjectIdRef.current === projectId) setRevisions(items);
    });
  }

  async function restoreRevision(id: string) {
    await run(async () => {
      const response = await api.post<ProjectMutationResponse<ProjectType, unknown>>(
        withRevision(`/api/projects/${projectId}/revisions/${id}/restore`, revision)
      );
      await onProjectMutation(response);
      setMessage("リビジョンを復元しました");
      const items = await fetchRevisions(projectId);
      if (activeProjectIdRef.current === projectId) setRevisions(items);
    });
  }

  const scriptReady = Boolean(session && session.stages.script.data);
  const selectedLocalWork = localWorks.find((work) => work.work_id === knowledgeWorkId);
  const canGenerate = useMemo(() => {
    const result: Record<StageName, boolean> = { brief: false, plot: false, pages: false, script: false };
    if (!session) return result;
    // 前段階が生成済み（データあり）なら次段階を生成できる。承認は不要。
    result.brief = true;
    result.plot = session.stages.brief.data !== null;
    result.pages = session.stages.plot.data !== null;
    result.script = session.stages.pages.data !== null;
    return result;
  }, [session]);

  return (
    <section className="story-panel">
      <div className="panel-message">{message}</div>
      {genProgress && (
        <div className="gen-progress" role="status" aria-live="polite">
          <div className="gen-progress-meta">
            <span className="gen-progress-spinner" aria-hidden="true" />
            生成中… {genProgress.chars > 0 ? `${genProgress.chars}文字を受信` : "モデルの応答を待機"} ・ 経過{" "}
            {genProgress.seconds}秒
          </div>
          {genProgress.tail && <pre className="gen-progress-tail">…{genProgress.tail}</pre>}
        </div>
      )}
      {llm && (
        <div className={`llm-band ${llm.provider !== "stub" && llm.connected ? "ok" : "ng"}`}>
          <strong>
            LLM: {llm.provider}
            {llm.model ? ` / ${llm.model}` : ""}
          </strong>
          <span>{llm.message}</span>
          {llm.provider === "stub" ? (
            <strong>警告: スタブ生成では実LLMの品質を確認できません。</strong>
          ) : !llm.connected ? (
            <strong>警告: 外部LLMを利用できないため、ストーリー生成は実行できません。</strong>
          ) : null}
          <button disabled={busy} onClick={() => void refreshLlm()}>
            ロード済みモデルを再検出
          </button>
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
        <div className="story-bulk-actions">
          <button className="primary" onClick={() => void generateAllStages()} disabled={busy}>
            企画→コマ台本を一括生成
          </button>
          <small className="hint">現在の各段階を順番に再生成します。</small>
        </div>
      )}

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
                {stage.warnings && stage.warnings.length > 0 && (
                  <ul className="stage-warnings" aria-label="編集チェックの警告">
                    {stage.warnings.map((warning, index) => (
                      <li key={index}>{warning}</li>
                    ))}
                  </ul>
                )}
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
                </div>
              </article>
            );
          })}
        </div>
      )}

      {session && (
        <div className="story-apply">
          <button className="primary" onClick={applySession} disabled={busy || !scriptReady}>
            プロジェクトへ適用
          </button>
          {!scriptReady && <small className="hint">コマ台本まで生成すると適用できます</small>}
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
