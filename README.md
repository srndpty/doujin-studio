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
```

MVPではComfyUI接続やワークフロー投入に失敗してもstub画像へフォールバックします。

## 主なAPI

- `GET /api/health`
- `POST /api/projects`
- `GET /api/projects`
- `GET /api/projects/{id}`
- `POST /api/projects/{id}/generate-name`
- `PUT /api/projects/{id}/manga-json`
- `POST /api/projects/{id}/render`
- `POST /api/projects/{id}/export/cbz`
- `GET /api/assets/{asset_id}`
