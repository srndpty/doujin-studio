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

## 進捗メモ

- 全更新APIをCAS化（`ProjectMutationService` 経由）。`save_manga_json` 廃止。
  `update_manga_json` は `revision` 必須。CAS callbackは純粋なJSON変更のみとし、
  レンダリング/CBZ等の副作用は確定manga/revisionに対しcommit後に1回だけ実行する。
- 構成全置換（ネーム再生成・ストーリー適用・リビジョン復元）は `generation_epoch` を進め、
  進行中ジョブを停止する。生成ジョブは開始時epochを保持し、候補保存時に世代不一致なら
  破棄してjobをcancelledにする（旧プロンプト候補の新作品への混入防止）。
- 生成登録（generate-image / generation-jobs / batch）は panel queued化・JobRecord追加・
  revision更新を `GenerationService.enqueue()` で単一CASトランザクション化。初回epochを
  CAS条件へ固定し、active panelのSQLite部分一意インデックスでも二重登録を防ぐ。
- PNGは描画入力hash付きの不変assetへ出力し、描画成功後に最新入力hashが一致した場合だけ
  `done`をCAS確定する。CBZも全ページ描画・アーカイブ成功後まで状態を進めない。
- overlay画像/maskも正規化PNGの内容hash付き不変assetへ保存する。全文JSON保存でページ・
  コマ順・読み順が変わった場合は`generation_epoch`を進め、旧ComfyUI promptも停止する。
- 旧形式の`done`ページ（`render_asset`/`render_hash`なし）は初回ロード時に`pending`へ移行する。
- 生成ジョブは構造epochに加えて、実生成panelと参照画像内容の`generation_input_hash`を保持し、
  候補保存前に不一致なら古い候補を破棄する。
- CBZ確定は開始時revision/epochの厳密CASとし、production statusは不変PNGの実在・名前・
  現在の描画hash一致まで検証する。
- 複数候補ジョブが入力変更でstaleになった場合は、そのjob自身が保存した候補だけをロール
  バックする。不変PNG/CBZ公開は上書き禁止と所有権記録を行い、競合cleanupを分離する。
- フロントの非同期project更新は選択中project IDと一致する場合だけManga JSON・revision・
  production status・job history・page assetへ反映する。
- 残課題: フロントの三者マージUI（現状は409時に最新採用reload）。また、不変assetは
  current JSON・project revision・候補・CBZ manifestの参照を走査する猶予付きGCが必要。
