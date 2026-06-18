import { FormEvent, useEffect, useMemo, useState } from "react";

type Dialogue = {
  speaker: string;
  text: string;
  balloon: string;
  position: string;
};

type Panel = {
  panel_id: string;
  bbox: [number, number, number, number];
  shot: string;
  camera: string;
  location_id: string;
  characters: string[];
  prompt: string;
  image_asset: string | null;
  dialogue: Dialogue[];
  sfx: { text: string; position: string; style: string }[];
  generation: {
    backend: string;
    prompt: string;
    negative_prompt: string;
    seed: number;
    workflow_id: string | null;
    model_notes: string;
    status: string;
    message: string;
  };
};

type MangaProject = {
  title: string;
  work_name: string;
  premise: string;
  target_pages: number;
  characters: { id: string; display_name: string; role: string; speech_style: string; visual_notes: string }[];
  pages: { page: number; theme: string; layout_template: string; panels: Panel[] }[];
};

type Project = {
  id: string;
  title: string;
  work_name: string;
  manga_json: MangaProject;
};

type ProjectSummary = {
  id: string;
  title: string;
  work_name: string;
  updated_at: string;
};

const api = {
  async get<T>(path: string): Promise<T> {
    const response = await fetch(path);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async post<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async put<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
};

export function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selected, setSelected] = useState<Project | null>(null);
  const [selectedPage, setSelectedPage] = useState(1);
  const [jsonText, setJsonText] = useState("");
  const [pageAssets, setPageAssets] = useState<string[]>([]);
  const [message, setMessage] = useState("準備完了");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    title: "テスト本",
    work_name: "サンプル作品",
    character_a: "キャラA",
    character_b: "キャラB",
    situation: "放課後の部室で差し入れを選ぶ",
    ending_direction: "小さな勘違いで笑って終わる"
  });

  const currentPage = useMemo(() => {
    return selected?.manga_json.pages.find((page) => page.page === selectedPage) ?? null;
  }, [selected, selectedPage]);

  useEffect(() => {
    void refreshProjects();
  }, []);

  async function refreshProjects() {
    const list = await api.get<ProjectSummary[]>("/api/projects");
    setProjects(list);
  }

  async function runTask(task: () => Promise<void>) {
    setBusy(true);
    try {
      await task();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "処理に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    await runTask(async () => {
      const project = await api.post<Project>("/api/projects", {
        title: form.title,
        work_name: form.work_name
      });
      setSelected(project);
      setSelectedPage(1);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets([]);
      await refreshProjects();
      setMessage("プロジェクトを作成しました");
    });
  }

  async function loadProject(id: string) {
    await runTask(async () => {
      const project = await api.get<Project>(`/api/projects/${id}`);
      setSelected(project);
      setSelectedPage(1);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setPageAssets([]);
      setMessage("プロジェクトを読み込みました");
    });
  }

  async function generateName() {
    if (!selected) return;
    await runTask(async () => {
      const project = await api.post<Project>(`/api/projects/${selected.id}/generate-name`, {
        work_name: form.work_name,
        character_a: form.character_a,
        character_b: form.character_b,
        situation: form.situation,
        ending_direction: form.ending_direction
      });
      setSelected(project);
      setSelectedPage(1);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setMessage("4ページネームを生成しました");
      await refreshProjects();
    });
  }

  async function saveJson() {
    if (!selected) return;
    await runTask(async () => {
      const parsed = JSON.parse(jsonText) as MangaProject;
      const project = await api.put<Project>(`/api/projects/${selected.id}/manga-json`, parsed);
      setSelected(project);
      setJsonText(JSON.stringify(project.manga_json, null, 2));
      setMessage("Manga JSONを保存しました");
    });
  }

  async function renderPages() {
    if (!selected) return;
    await runTask(async () => {
      const response = await api.post<{ page_assets: string[]; manga_json: MangaProject }>(`/api/projects/${selected.id}/render`);
      setPageAssets(response.page_assets);
      const project = { ...selected, manga_json: response.manga_json };
      setSelected(project);
      setJsonText(JSON.stringify(response.manga_json, null, 2));
      setMessage("ページをレンダリングしました");
    });
  }

  async function exportCbz() {
    if (!selected) return;
    await runTask(async () => {
      const response = await api.post<{ cbz_asset: string }>(`/api/projects/${selected.id}/export/cbz`);
      setMessage(`CBZを書き出しました: /api/assets/${response.cbz_asset}`);
    });
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>Local Doujin Studio</h1>
        <form onSubmit={createProject} className="stack">
          <label>
            タイトル
            <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} />
          </label>
          <label>
            作品名
            <input value={form.work_name} onChange={(event) => setForm({ ...form, work_name: event.target.value })} />
          </label>
          <button disabled={busy}>新規作成</button>
        </form>
        <div className="project-list">
          <h2>プロジェクト</h2>
          {projects.map((project) => (
            <button key={project.id} className={selected?.id === project.id ? "selected" : ""} onClick={() => void loadProject(project.id)}>
              <span>{project.title}</span>
              <small>{project.work_name || "作品名未設定"}</small>
            </button>
          ))}
        </div>
      </aside>

      <main className="workspace">
        <header className="toolbar">
          <div>
            <strong>{selected?.title ?? "プロジェクト未選択"}</strong>
            <span>{message}</span>
          </div>
          <div className="actions">
            <button onClick={generateName} disabled={!selected || busy}>ネーム生成</button>
            <button onClick={saveJson} disabled={!selected || busy}>JSON保存</button>
            <button onClick={renderPages} disabled={!selected || busy}>レンダリング</button>
            <button onClick={exportCbz} disabled={!selected || busy}>CBZ出力</button>
          </div>
        </header>

        <section className="generator">
          <label>
            キャラA
            <input value={form.character_a} onChange={(event) => setForm({ ...form, character_a: event.target.value })} />
          </label>
          <label>
            キャラB
            <input value={form.character_b} onChange={(event) => setForm({ ...form, character_b: event.target.value })} />
          </label>
          <label>
            シチュエーション
            <input value={form.situation} onChange={(event) => setForm({ ...form, situation: event.target.value })} />
          </label>
          <label>
            オチの方向
            <input value={form.ending_direction} onChange={(event) => setForm({ ...form, ending_direction: event.target.value })} />
          </label>
        </section>

        <section className="content-grid">
          <div className="preview">
            <div className="tabs">
              {[1, 2, 3, 4].map((page) => (
                <button key={page} className={selectedPage === page ? "active" : ""} onClick={() => setSelectedPage(page)}>
                  {page}p
                </button>
              ))}
            </div>
            <div className="page-frame">
              {pageAssets[selectedPage - 1] ? (
                <img src={`/api/assets/${pageAssets[selectedPage - 1]}`} alt={`${selectedPage}ページ`} />
              ) : (
                <div className="page-placeholder">
                  <strong>{currentPage ? `${currentPage.page}ページ` : "未生成"}</strong>
                  <span>{currentPage?.theme ?? "ネーム生成後にプレビューできます"}</span>
                </div>
              )}
            </div>
            <div className="panel-list">
              {currentPage?.panels.map((panel) => (
                <article key={panel.panel_id}>
                  <strong>{panel.panel_id}</strong>
                  <span>{panel.shot}</span>
                  <small>{panel.generation.status}</small>
                </article>
              ))}
            </div>
          </div>

          <div className="json-pane">
            <h2>Manga JSON</h2>
            <textarea value={jsonText} onChange={(event) => setJsonText(event.target.value)} spellCheck={false} />
          </div>
        </section>
      </main>
    </div>
  );
}
