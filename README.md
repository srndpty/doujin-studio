# Local Doujin Studio

ローカルで4ページ短編マンガのネーム生成、ページレンダリング、PNG/CBZ出力を行うMVPです。画像生成はstubを標準にし、ComfyUIが起動している場合だけ外部APIへ接続できます。

## 構成

- `backend/`: FastAPI、SQLite、Manga JSON、レンダリング、CBZ出力
- `frontend/`: React/Vite/TypeScriptのローカルUI
- `data/`: SQLiteデータベース
- `exports/`: PNG/CBZ出力
- `docs/`: 補足ドキュメント

## バックエンド起動

```powershell
uv sync
uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

ComfyUI実生成を使う場合は、ComfyUIを起動してから次を実行します。

```powershell
.\scripts\start-backend-comfy.ps1
```

## フロントエンド起動

```powershell
Set-Location frontend
npm install
npm run dev
```

ブラウザで`http://127.0.0.1:5173`を開きます。

## テスト

```powershell
uv run pytest
Set-Location frontend
npm run build
```

## 編集品質設定

コマごとに以下を編集できます。

- 生成サイズ: workflow既定、またはコマごとのwidth/height
- 画像配置: `cover`はコマ枠を埋める、`contain`は画像全体を表示する
- crop基準: `center`、`top`、`bottom`、`left`、`right`
- 写植: 台詞、吹き出し位置/サイズ、フォントサイズ、最大行数

画像生成promptには、文字や吹き出しを画像側に描かせないための文言を自動付与します。台詞と吹き出しはアプリ側レンダラで後から合成します。

全コマ共通promptも設定できます。

- `anime-preview3-base向け初期値`: anime系workflow向けの品質タグとnegative promptを入力します
- `全コマに反映`: 共通positiveを各コマpromptへ追加し、共通negativeを各コマへ反映します
- `4ページ仮設定`: 1p導入、2p会話、3pリアクション、4pオチ用の仮promptと生成サイズを入れます

画像生成はバックグラウンドジョブとして実行されます。ComfyUIのWebSocketイベントからKSamplerの進捗と実行ノードを取得し、UIの進捗バーへ反映します。WebSocket接続が使えない場合はHTTPポーリングへ自動的に切り替わります。

コマごとに1～4件の画像候補を生成できます。候補には画像、prompt、negative prompt、seed、backend、prompt IDを保存し、候補ギャラリーから採用画像を切り替えられます。新しい候補は自動採用されますが、以前の候補画像は削除されません。

生成ジョブはMVPとしてバックエンドプロセス内で管理します。バックエンド再起動時に実行中ジョブは復元されないため、Uvicornは単一workerで起動してください。

## ComfyUI連携

ComfyUIはこのリポジトリに同梱しません。外部で起動しているComfyUIに接続します。

```powershell
$env:IMAGE_BACKEND = "comfyui"
$env:COMFYUI_BASE_URL = "http://127.0.0.1:8001"
$env:COMFYUI_WORKFLOW_PATH = "workflows/default.workflow_api.json"
```

ComfyUI画面で目的のworkflowを作り、`File -> Export (API)`で書き出したJSONを`workflows/default.workflow_api.json`として配置してください。サンプルとして`workflows/default.workflow_api.example.json`を同梱しています。

既定のノードIDはComfyUIの標準的なサンプルworkflowに合わせています。別workflowを使う場合は、以下を`.env`またはPowerShell環境変数で指定してください。

```powershell
$env:COMFYUI_POSITIVE_NODE_ID = "11"
$env:COMFYUI_NEGATIVE_NODE_ID = "12"
$env:COMFYUI_SEED_NODE_ID = "19"
$env:COMFYUI_WIDTH_NODE_ID = "28"
$env:COMFYUI_HEIGHT_NODE_ID = "28"
$env:COMFYUI_SAVE_PREFIX_NODE_ID = "46"
```

MVPではComfyUI接続、workflow読込、生成待機、画像取得のいずれかに失敗してもstub画像へフォールバックします。NoobAI、Illustrious、各種LoRAなどのモデル名や配置はユーザーのComfyUI環境に依存します。

## 主なAPI

- `GET /api/health`
- `GET /api/comfyui/status`
- `POST /api/projects`
- `GET /api/projects`
- `GET /api/projects/{id}`
- `POST /api/projects/{id}/generate-name`
- `PUT /api/projects/{id}/manga-json`
- `POST /api/projects/{id}/render`
- `POST /api/projects/{id}/panels/{panel_id}/generate-image`
- `POST /api/projects/{id}/panels/{panel_id}/generation-jobs`
- `GET /api/generation-jobs/{job_id}`
- `POST /api/generation-jobs/{job_id}/cancel`
- `WS /api/generation-jobs/{job_id}/ws`
- `POST /api/projects/{id}/panels/{panel_id}/candidates/{candidate_id}/select`
- `POST /api/projects/{id}/panels/{panel_id}/render-page`
- `POST /api/projects/{id}/export/cbz`
- `GET /api/assets/{asset_id}`
