# リファクタリング方針（長期保守性）

レビューで指摘された長期保守性の改善は、機能の正しさに直結する不具合修正
（楽観ロック契約の統一・キャンセル回帰・候補/ページ整合）を優先して対応済み。
ここでは、まだ着手していない構造的リファクタリングの方針と順序を記録する。
いずれもリグレッションリスクが大きいため、段階的に進める。

## 1. バックエンドの責務分割（最優先）

現在 `backend/app/main.py` がルーティング・HTTPエラー変換・JSON永続化・CAS・
画像アップロード検証・生成ジョブ調停・ComfyUI停止・レンダリング・知識DB操作までを
抱えている。今回の「更新APIごとのrevision漏れ」もこの集中構造が一因。

抽出順序（依存の少ない順）:

1. `ProjectRepository` — `ProjectRecord` の取得と CAS 更新（`UPDATE ... WHERE id AND revision`）。
   現状の `save_manga_json()` / `update_panel_in_latest()` の DB アクセスをここへ集約する。
2. `ProjectMutationService` — revision採番・JSON parse/save・ページ dirty 化・
   `invalidate_changed_pages()` / `page_render_signature()` を持つ。全更新APIはこの
   サービス経由にし、応答へ revision を含める契約を一箇所で保証する。
3. `GenerationService` — ジョブ作成・候補保存・選択状態（開始時選択の保護）・
   キャンセル（`JobManager.cancel()` を唯一の状態遷移窓口とする）・ComfyUI停止。
4. `routers/*` — HTTP入出力だけを担当（Pydanticモデル↔サービス呼び出し）。

ねらい: 「全文保存はCAS / 個別操作はread-modify-write / 応答revisionの有無が経路依存」
という三種類混在を、サービス層の単一契約へ寄せる。理想形は次の共通応答:

```
ProjectMutationResponse<T> = { project: ProjectDetail; result: T }
```

ユーザー起点の変更は `expected_revision` 必須、バックグラウンド生成は対象パネル限定の
CASリトライ、という二層構造にする。

## 2. フロントエンドのAPI境界集約

`App.tsx` の `api`、`StoryPanel`、`KnowledgePanel` に通信・例外処理が分散している。
`api/client.ts` と `useProjectMutation()` を新設し、

- 通信エラーの正規化（`ApiError`）
- 409競合処理（最新採用 reload。将来は base/local/latest の三者マージUIへ）
- `ProjectDetail`（manga_json + revision）の反映
- 更新中UI状態
- revision付き保存

を集約する。現状は `App.tsx` 内の `putMangaJson()` / `applyMutatedManga()` で
revision契約を満たしているが、`StoryPanel` / `KnowledgePanel` までは未統一。
`App.tsx` の画面単位分割は client 集約の後に進める方が低リスク。

## 3. DBスキーマのマイグレーション基盤

SQLiteは既存テーブルへ FK を後付けできないため、現在モデルに定義した
`ForeignKey(..., ondelete="CASCADE")` は新規DBにのみ適用される（`ensure_columns()`
にコメント済み）。`ensure_columns()` に ALTER を積み増す方式は、テーブル再作成・
インデックス変更・データ変換が必要になった時点で限界が来る。

Alembic 全面導入の前段として、最低限:

- `schema_version` を持つメタテーブル
- バージョン番号で分岐する段階的 migration 関数（SQLite のテーブル再作成を含められる）

を用意し、既存DBへも FK/cascade を導入できる経路を確保する。

## 4. ジョブ再起動復旧の本格対応

現状（MVP）: 再起動時、`running` だったジョブは二重投入を避けるため
`error: 再実行してください` にし、`queued` のみ再開する（`JobManager.restore_pending()`）。

本格対応: 候補単位の prompt ID・生成試行・出力先を永続化し、ComfyUI の queue/history と
照合して「実は完了していた／まだ走っている」を判定して復旧する。
