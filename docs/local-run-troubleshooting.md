# ローカル起動ランブック / トラブルシューティング

実機（ComfyUI 実生成＋実 LLM）で e2e 確認するときの起動手順と、よくある詰まりの対処をまとめる。

## プロセス構成（最大4つ）

| 役割 | 既定ポート | 起動 | 必須度 |
| --- | --- | --- | --- |
| ComfyUI（画像生成） | 8188（Desktop版は自動でずれる→後述） | アプリ起動 or `scripts\start-comfy-headless.ps1` | 画像生成に必須 |
| backend（API） | 8000 | `scripts\start-backend-comfy.ps1` | 必須 |
| frontend（UI） | 5173 | `frontend/` で `npm run dev` | 必須 |
| LLM（台本生成） | 1234 | LM Studio / Ollama 等で OpenAI 互換 API | 実台本に必須（無いと stub 退避） |

起動順は **ComfyUI → backend → frontend** が無難（backend が起動時に ComfyUI 疎通を見るため）。

## 起動の早見表

```powershell
# 1) ComfyUI（Desktop版はアプリを起動するだけ。サーバが自動で立つ）
& "C:\Users\<you>\AppData\Local\Programs\ComfyUI\Comfy Desktop\Comfy Desktop.exe"
#    実際のポートを確認（8188 とは限らない。下記「ポート自動ずれ」参照）
Invoke-WebRequest -Uri "http://127.0.0.1:8188/system_stats" -UseBasicParsing | Select-Object StatusCode

# 2) backend（ComfyUI の実ポートに合わせる）
.\scripts\start-backend-comfy.ps1 -ComfyBaseUrl "http://127.0.0.1:8188"
#    接続確認（"connected":true を確認）
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/comfyui/status" -UseBasicParsing).Content

# 3) frontend（必ず frontend ディレクトリで）
cd frontend; npm run dev      # → http://127.0.0.1:5173

# 4) LLM（任意・実台本用）。LM Studio 等で :1234 にモデルをロードして起動
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/llm/status" -UseBasicParsing).Content
```

## よくあるエラーと対処

### DB: `SchemaMigrationError: このアプリより新しいDB schema versionが適用済みです`
- 原因: 別ブランチ/旧設計でローカル DB に余分な migration 行が残った（前方互換なし）。`data/local-doujin-studio.db` はローカル専用（git 管理外）。
- 対処A（データ保持・別DBで起動）:
  ```powershell
  $env:DATABASE_URL = "sqlite:///data/<branch>.db"
  .\scripts\start-backend-comfy.ps1 -ComfyBaseUrl "http://127.0.0.1:8188"
  ```
- 対処B（その DB を使い続ける）: バックアップしてから余分な行を削除。
  ```powershell
  Copy-Item data/local-doujin-studio.db ("data/local-doujin-studio.db.bak-" + (Get-Date -Format yyyyMMdd-HHmmss))
  uv run python -c "import sqlite3;c=sqlite3.connect('data/local-doujin-studio.db');c.execute('DELETE FROM schema_migrations WHERE version > 1');c.commit();print(c.execute('SELECT version,name FROM schema_migrations').fetchall());c.close()"
  ```
  ※ 削除する migration がスキーマを実際に変更していた場合は不整合の恐れあり。本リポジトリの v2(`project_deletion_fences`) は現行コードがファイルベース実装へ置換済みで未使用のため、行削除のみで安全だった。
- 注意: ブランチを行き来して同じ DB を使うと再発する。ブランチごとに `DATABASE_URL` を分けるのが安全。

### ComfyUI: `start-comfy-headless.ps1` が `main.py が見つかりません` で失敗
- 原因: ComfyUI **Desktop（ToDesktop パッケージ）版**は本体が `app.asar` 内に同梱され、`main.py` を直接起動できない。このスクリプトはソース版/ポータブル版向け。
- 対処: **Desktop アプリをそのまま起動**する（アプリがサーバを内蔵）。headless が必要なら、ソース版（`git clone` した ComfyUI）かポータブル版を用意し、その**フォルダ**（`.exe` ではない）を引数に渡す。
  ```powershell
  .\scripts\start-comfy-headless.ps1 -ComfyPath C:\path\to\ComfyUI   # 引用符・.exe を付けない
  ```
- 対話プロンプトで値を聞かれた場合、**引用符を付けない**（`"C:\..."` と入れると `"C` がドライブ名扱いになる）。

### ComfyUI: `8188` が接続拒否なのにアプリは起動している（ポート自動ずれ）
- 原因: Desktop 版は既定ポートが空いていないと**自動で別ポート**（例: 8000 が埋まっていて 8001）へずれる。
- 対処: 実ポートを特定して backend を合わせる。
  ```powershell
  # python が LISTEN しているポートを探す
  Get-NetTCPConnection -State Listen | Where-Object { (Get-Process -Id $_.OwningProcess).ProcessName -match 'python' } | Select LocalPort,OwningProcess
  # /system_stats が 200 を返すポートが ComfyUI
  Invoke-WebRequest -Uri "http://127.0.0.1:8001/system_stats" -UseBasicParsing | Select StatusCode
  # そのポートで backend を起動
  .\scripts\start-backend-comfy.ps1 -ComfyBaseUrl "http://127.0.0.1:8001"
  ```
- 固定したい場合: Desktop の設定でサーバポートを 8188 に指定するか、backend を先に 8000 で起動してから Desktop を後から上げる。

### frontend: `npm error enoent ... package.json`（リポジトリ直下で `npm run dev`）
- 原因: フロントは `frontend/` 配下。直下に `package.json` は無い。
- 対処: `cd frontend; npm run dev`、または `npm --prefix frontend run dev`。初回は `npm install`。

### 台本が固定文（stub）になる
- 原因: LLM 未起動。backend は既定で `http://127.0.0.1:1234/v1`（OpenAI 互換）を見る。
- 対処: LM Studio 等でモデルをロードしサーバ起動。`/api/llm/status` の `connected` を確認。

### 生成時に `missing_nodes` / 生成エラー
- 原因: `workflows/default.workflow_api.json` のノード構成（positive=11 / negative=12 / seed=19 / size=28 / save=46）と、ComfyUI 側にロードされた workflow・モデルが不一致。キャラ同一性（LoRA・参照画像）を使うには対応ノードを持つ workflow が別途必要。
- 対処: `/api/comfyui/status` の `missing_nodes` を確認し、workflow とノード ID（`start-backend-comfy.ps1` の `COMFYUI_*_NODE_ID`）を合わせる。

## 確認用エンドポイント

- ComfyUI 疎通: `GET http://127.0.0.1:8000/api/comfyui/status` → `"connected":true`
- LLM 疎通: `GET http://127.0.0.1:8000/api/llm/status` → `"connected":true`（false は stub 動作）
- ComfyUI 直接: `GET http://127.0.0.1:<port>/system_stats` → 200
