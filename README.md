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

## ComfyUI連携

ComfyUIはこのリポジトリに同梱しません。外部で起動しているComfyUIに接続します。

```powershell
$env:IMAGE_BACKEND = "comfyui"
$env:COMFYUI_BASE_URL = "http://127.0.0.1:8188"
$env:COMFYUI_WORKFLOW_PATH = "workflows/default.workflow_api.json"
```

ComfyUI画面で目的のworkflowを作り、`File -> Export (API)`で書き出したJSONを`workflows/default.workflow_api.json`として配置してください。サンプルとして`workflows/default.workflow_api.example.json`を同梱しています。

既定のノードIDはComfyUIの標準的なサンプルworkflowに合わせています。別workflowを使う場合は、以下を`.env`またはPowerShell環境変数で指定してください。

```powershell
$env:COMFYUI_POSITIVE_NODE_ID = "6"
$env:COMFYUI_NEGATIVE_NODE_ID = "7"
$env:COMFYUI_SEED_NODE_ID = "3"
$env:COMFYUI_WIDTH_NODE_ID = "5"
$env:COMFYUI_HEIGHT_NODE_ID = "5"
$env:COMFYUI_SAVE_PREFIX_NODE_ID = "9"
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
- `POST /api/projects/{id}/export/cbz`
- `GET /api/assets/{asset_id}`
