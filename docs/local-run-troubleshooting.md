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

## 1コマンド起動（dev-up.ps1）

ComfyUI のポートを自動検出（または headless 起動）し、backend と frontend を各ウィンドウで立ち上げる。
LLM は VRAM 都合で別運用のため起動しない。

```powershell
# 既に動いている ComfyUI(Desktop等)を検出して backend+frontend を起動
.\scripts\dev-up.ps1

# ソース版 ComfyUI を Desktop の basePath を再利用して headless 起動してから一括起動
.\scripts\dev-up.ps1 -ComfyPath C:\tools\ComfyUI -ComfyBasePath "C:\Users\<you>\Documents\ComfyUI"
```

`-ProbePorts 8188,8001,8000` で検出候補ポートを調整できる（Desktop 版はポートが自動でずれるため）。

Windows Terminal がある環境では、**1 ウィンドウの複数タブ**（ComfyUI/ollama/backend/frontend）にまとめて開く（手動の D&D 連結が不要）。
各タブは `-EncodedCommand` で渡すため、コマンド内の `;` も正しく保持される。タブにまとめず従来どおり別ウィンドウで開きたい場合は `-SeparateWindows` を付ける。
headless 起動時は空き候補ポートを選び（backend ポートは除外）、backend タブは ComfyUI 疎通を待ってから起動する。

`dev-up.ps1` の純粋ヘルパーは `scripts/dev-lib.ps1` に分離し、Pester で検証している:

```powershell
Invoke-Pester -Path tests/dev-lib.Tests.ps1
```

## VRAM の排他（LLM ⇄ ComfyUI）

LLM が VRAM 重い場合、プロセスを落とすのではなく **モデルだけ VRAM から退避**するのが堅実。

### おすすめ: Ollama で完全自動（手動操作ゼロ）

2つの自動化を組み合わせると、LLM と ComfyUI が VRAM に同居しなくなり、手動の解放操作が不要になる。

1. **LLM 側**: Ollama を `OLLAMA_KEEP_ALIVE=0` で起動 → 各台本生成の直後にモデルを VRAM から自動退避。
2. **ComfyUI 側**: backend の `COMFYUI_FREE_BEFORE_LLM=1` → 台本生成の直前に ComfyUI `/free` を自動実行し VRAM 解放。

`dev-up.ps1 -Ollama` がこの両方を自動で設定する（Ollama serve を keep_alive=0 で起動し、backend へ `LLM_*` と `COMFYUI_FREE_BEFORE_LLM=1` を渡す）。

```powershell
# モデルを一度取得（初回のみ・サイズ大）
ollama pull qwen2.5:14b
# ComfyUI(検出) + Ollama(keep_alive=0) + backend(自動/free) + frontend を一括起動
.\scripts\dev-up.ps1 -Ollama -OllamaModel qwen2.5:14b
```

注意: 既に Ollama がサービス/デスクトップアプリとして 11434 で起動している場合、本スクリプトはその環境変数を変更できない（アプリ既定は `OLLAMA_KEEP_ALIVE=5m`）。
VRAM 自動退避を効かせるには次のどちらか:

```powershell
# A) アプリ運用のまま keep_alive=0 を効かせる（推奨・確実）
setx OLLAMA_KEEP_ALIVE 0
#    → タスクトレイの Ollama を Quit して再起動。生成直後に確認:
ollama ps          # UNTIL が即時/Stopping… になっていれば OK

# B) アプリを Quit して serve をスクリプトに任せる
.\scripts\dev-up.ps1 -Ollama   # keep_alive=0 付きで ollama serve を起動
```

`ollama pull <model>` はグローバル（保存先 `~/.ollama/models`）でターミナル非依存。`ollama list` で取得済みを確認できる。
モデル名は実在タグを指定する（`ollama list` / ollama.com/library で確認）。

### 手動/個別の方法

- **LLM 側**: LM Studio を使うならモデル TTL / JIT auto-evict を短く設定。
- **ComfyUI 側**: `POST /free {"unload_models":true,"free_memory":true}` でサーバを落とさず解放。
- ヘルパー（半自動）: `scripts\gpu-free.ps1 -ComfyBaseUrl http://127.0.0.1:8001 [-OllamaModel <name>]`

## Desktop → headless 移行（model/workflow を消さない）

Desktop は全データを basePath（例 `C:\Users\<you>\Documents\ComfyUI`：`models`/`custom_nodes`/`user`(UIワークフロー)/`input`/`output`）に保持している。
ソース版 ComfyUI を**同じ basePath に向けるだけ**で、データを移動・複製・削除せず再利用できる。

```powershell
git clone https://github.com/comfyanonymous/ComfyUI C:\tools\ComfyUI
# venv 作成＋依存(torch CUDA等)導入は別途
.\scripts\start-comfy-headless.ps1 -ComfyPath C:\tools\ComfyUI -BasePath "C:\Users\<you>\Documents\ComfyUI"
```

`-BasePath` 指定で `--base-directory/--user-directory/--input/output-directory` と Desktop の `extra_models_config.yaml` を引き継ぐ。
注意: `custom_nodes` の Python 依存は新 venv へ入れ直しが要る場合がある（ComfyUI-Manager で再導入可）。バニラノードはそのまま動く。

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

### ComfyUI(headless): ポート確認後に bind 失敗する
- 原因: `dev-up.ps1` は空きポートを選んでから headless ComfyUI を起動するが、確認と bind の
  間に別プロセスが同じポートを取得しうる（TOCTOU race）。
- 対処: `-ProbePorts` に別候補を足して再実行する（例 `-ProbePorts 8190,8189,8188`）。ComfyUI
  タブのログに bind/address-in-use 系のエラーが出ていればこれ。

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

### `LLM応答がタイムアウトしました`（特に台本ステージ）
- 原因: ローカル大型モデルは出力が長く、既定 `LLM_TIMEOUT_SECONDS=180`（3分）では台本生成が
  間に合わない（Ollama ログに `500 | 3m0s | POST /v1/chat/completions` が出る）。台本は最大3回
  リトライするため、各回が3分で打ち切られる。
- 対処: タイムアウトを延ばす。`dev-up.ps1` は `-LlmTimeout 600`（既定600秒）で渡す。
  既に起動中の backend だけ直すなら、backend タブで Ctrl+C 後:
  ```powershell
  $env:LLM_TIMEOUT_SECONDS = "600"
  & .\scripts\start-backend-comfy.ps1 -ComfyBaseUrl "http://127.0.0.1:8001"
  ```
- それでも遅い場合: より小型・高速なモデル（例 `qwen2.5:14b`）にする、知識の参照件数や
  `LLM_MAX_CONTEXT_CHARS` を絞ってプロンプト長を減らす。

### 生成時に `missing_nodes` / 生成エラー
- 原因: `workflows/default.workflow_api.json` のノード構成（positive=11 / negative=12 / seed=19 / size=28 / save=46）と、ComfyUI 側にロードされた workflow・モデルが不一致。キャラ同一性（LoRA・参照画像）を使うには対応ノードを持つ workflow が別途必要。
- 対処: `/api/comfyui/status` の `missing_nodes` を確認し、workflow とノード ID（`start-backend-comfy.ps1` の `COMFYUI_*_NODE_ID`）を合わせる。

## 確認用エンドポイント

- ComfyUI 疎通: `GET http://127.0.0.1:8000/api/comfyui/status` → `"connected":true`
- LLM 疎通: `GET http://127.0.0.1:8000/api/llm/status` → `"connected":true`（false は stub 動作）
- ComfyUI 直接: `GET http://127.0.0.1:<port>/system_stats` → 200
