import { useEffect, useState } from "react";

type Usage = "required" | "reference";
type DocType = "json" | "markdown" | "txt";

type KnowledgeSource = {
  id: string;
  work_name: string;
  title: string;
  doc_type: DocType;
  usage: Usage;
  chunk_count: number;
  created_at: string;
};

type KnowledgeChunk = {
  id: string;
  source_id: string;
  work_name: string;
  usage: Usage;
  kind: string;
  title: string;
  content: string;
  policy: string;
  tags: string[];
  position: number;
};

type SearchHit = { chunk: KnowledgeChunk; score: number; method: "trigram" | "like" };

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

export function KnowledgePanel({ defaultWorkName }: { defaultWorkName: string }) {
  const [workName, setWorkName] = useState(defaultWorkName);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [usage, setUsage] = useState<Usage>("reference");
  const [docType, setDocType] = useState<DocType>("txt");
  const [docTitle, setDocTitle] = useState("");
  const [docContent, setDocContent] = useState("");
  const [query, setQuery] = useState("");
  const [searchUsage, setSearchUsage] = useState<"" | Usage>("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [message, setMessage] = useState("作品名を指定して知識を登録できます");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setWorkName(defaultWorkName);
  }, [defaultWorkName]);

  async function run(task: () => Promise<void>) {
    setBusy(true);
    try {
      await task();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "処理に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  async function refreshSources() {
    if (!workName) {
      setSources([]);
      return;
    }
    setSources(await getJson<KnowledgeSource[]>(`/api/knowledge/sources?work_name=${encodeURIComponent(workName)}`));
  }

  useEffect(() => {
    void refreshSources();
  }, [workName]);

  async function importFiles(files: FileList) {
    await run(async () => {
      const payloadFiles = await Promise.all(
        Array.from(files).map(async (file) => ({ filename: file.name, content: await file.text() }))
      );
      await sendJson(`/api/knowledge/sources/import`, "POST", { work_name: workName, usage, files: payloadFiles });
      setMessage(`${payloadFiles.length}件のファイルを取り込みました`);
      await refreshSources();
    });
  }

  async function addDocument() {
    if (!docContent.trim()) {
      setMessage("ドキュメント本文を入力してください");
      return;
    }
    await run(async () => {
      await sendJson(`/api/knowledge/documents`, "POST", {
        work_name: workName,
        title: docTitle || "無題ドキュメント",
        doc_type: docType,
        usage,
        content: docContent
      });
      setDocContent("");
      setDocTitle("");
      setMessage("ドキュメントを登録しました");
      await refreshSources();
    });
  }

  async function deleteSource(id: string) {
    await run(async () => {
      await sendJson(`/api/knowledge/sources/${id}`, "DELETE");
      setMessage("知識ソースを削除しました");
      await refreshSources();
    });
  }

  async function search() {
    if (!query.trim()) return;
    await run(async () => {
      const response = await sendJson<{ hits: SearchHit[] }>(`/api/knowledge/search`, "POST", {
        work_name: workName,
        query,
        usage: searchUsage || undefined,
        limit: 20
      });
      setHits(response.hits);
      setMessage(`${response.hits.length}件ヒットしました`);
    });
  }

  return (
    <section className="knowledge-panel">
      <div className="panel-message">{message}</div>
      <div className="settings-grid">
        <label>
          作品名（全プロジェクト共有）
          <input value={workName} onChange={(event) => setWorkName(event.target.value)} placeholder="作品名" />
        </label>
        <label>
          区分
          <select value={usage} onChange={(event) => setUsage(event.target.value as Usage)}>
            <option value="required">required（必須条件）</option>
            <option value="reference">reference（参考情報）</option>
          </select>
        </label>
      </div>

      <div className="knowledge-import">
        <h3>ファイル取込</h3>
        <p className="hint">JSON / Markdown / TXT を選択して取り込みます。拡張子で種別を判定します。</p>
        <input
          type="file"
          multiple
          accept=".json,.md,.markdown,.txt"
          disabled={busy || !workName}
          onChange={(event) => {
            if (event.target.files?.length) void importFiles(event.target.files);
            event.target.value = "";
          }}
        />
      </div>

      <details className="knowledge-document">
        <summary>テキストを直接追加</summary>
        <label>
          タイトル
          <input value={docTitle} onChange={(event) => setDocTitle(event.target.value)} />
        </label>
        <label>
          種別
          <select value={docType} onChange={(event) => setDocType(event.target.value as DocType)}>
            <option value="txt">txt（文字数分割）</option>
            <option value="markdown">markdown（見出し分割）</option>
            <option value="json">json（kind/title/content/policy/tags）</option>
          </select>
        </label>
        <label>
          本文
          <textarea value={docContent} onChange={(event) => setDocContent(event.target.value)} spellCheck={false} rows={6} />
        </label>
        <button onClick={addDocument} disabled={busy || !workName}>登録</button>
      </details>

      <div className="knowledge-sources">
        <h3>登録済み知識（{sources.length}）</h3>
        {sources.length === 0 && <small className="hint">この作品名の知識はまだありません</small>}
        {sources.map((source) => (
          <article key={source.id} className={`source-card ${source.usage}`}>
            <div>
              <strong>{source.title}</strong>
              <small>{source.doc_type} / {source.usage === "required" ? "必須" : "参考"} / {source.chunk_count}チャンク</small>
            </div>
            <button onClick={() => void deleteSource(source.id)} disabled={busy}>削除</button>
          </article>
        ))}
      </div>

      <div className="knowledge-search">
        <h3>検索確認</h3>
        <div className="settings-grid">
          <label>
            キーワード
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="3文字以上はtrigram検索" />
          </label>
          <label>
            区分フィルタ
            <select value={searchUsage} onChange={(event) => setSearchUsage(event.target.value as "" | Usage)}>
              <option value="">すべて</option>
              <option value="required">required</option>
              <option value="reference">reference</option>
            </select>
          </label>
        </div>
        <button onClick={search} disabled={busy || !workName || !query.trim()}>検索</button>
        <div className="search-results">
          {hits.map((hit) => (
            <article key={hit.chunk.id} className="search-hit">
              <div className="hit-head">
                <strong>{hit.chunk.title || hit.chunk.kind || "（無題）"}</strong>
                <small>{hit.method} / {hit.score.toFixed(2)} / {hit.chunk.usage}</small>
              </div>
              <p>{hit.chunk.content.slice(0, 160)}</p>
              {hit.chunk.tags.length > 0 && <small className="tags">{hit.chunk.tags.join(", ")}</small>}
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
