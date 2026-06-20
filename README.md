# Local Doujin Studio

ローカルで4・8・16ページマンガのネーム生成、ページレンダリング、PNG/CBZ出力を行う制作システムです。画像生成はstubを標準にし、ComfyUIが起動している場合だけ外部APIへ接続できます。

## 構成

- `backend/`: FastAPI、SQLite、Manga JSON、レンダリング、CBZ出力
- `frontend/`: React/Vite/TypeScriptのローカルUI
- `data/`: SQLiteデータベース
- `exports/`: PNG/CBZ出力
- `docs/`: 補足ドキュメント

## バックエンド起動

```powershell
uv sync
uv run uvicorn backend.app.main:app --reload --reload-dir backend --host 127.0.0.1 --port 8000
```

`--reload-dir backend`でコード本体だけを監視します（`.venv`/`data`/`exports`を監視すると重く誤検知も増えるため）。

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
- `4ページ仮設定`: 1p導入、2p会話、3pリアクション、4pオチ用の仮promptと生成サイズを入れます

画像生成はバックグラウンドジョブとして実行されます。ComfyUIのWebSocketイベントからKSamplerの進捗と実行ノードを取得し、UIの進捗バーへ反映します。WebSocket接続が使えない場合はHTTPポーリングへ自動的に切り替わります。

コマごとに1～4件の画像候補を生成できます。候補には画像、prompt、negative prompt、seed、backend、prompt IDを保存し、候補ギャラリーから採用画像を切り替えられます。新しい候補は自動採用されますが、以前の候補画像は削除されません。

## キャラクタープロファイル

キャラクターごとに表示名、trigger prompt、外見タグ、衣装タグ、個別negativeを保存できます。各コマで登場キャラを選ぶと、生成直前に次の順でpromptを自動合成します。

1. 全コマ共通prompt
2. 登場キャラのtrigger、外見、衣装
3. コマ固有prompt

negative promptも全コマ共通、登場キャラ固有、コマ固有の順で合成します。同じタグは重複を除去し、実際に使用したpromptと登場キャラIDを画像候補へ保存します。

## LoRA・参照画像連携

キャラクタープロファイルにLoRA名とComfyUI workflow内の`LoraLoader`ノードIDを設定すると、生成時に次の入力を差し替えます。

- `lora_name`
- `strength_model`
- `strength_clip`

参照画像を登録し、参照画像ノードIDへworkflow内の`LoadImage`ノードIDを指定すると、画像をComfyUIの`/upload/image`へ送信して`inputs.image`を差し替えます。IP-AdapterやControlNet本体のノード接続はworkflow側で事前に構成してください。複数キャラを同じコマへ出す場合は、キャラごとに別のLoRA・LoadImageノードが必要です。

## 制作完了管理

制作状態は、コマの採用画像とページのレンダリング状態から判定します。

- `制作中`: 採用画像がないコマがある
- `レンダリング待ち`: 全コマ採用済みだが未レンダリングページがある
- `制作完了`: 全コマ採用済みかつ全ページレンダリング済み

ページ内一括生成は全ジョブをバックエンドへまとめて登録し、ComfyUIへ1件ずつ直列投入します。CBZ出力時に未採用コマが残っている場合は警告件数を表示します。

生成ジョブはSQLiteへ保存します。バックエンド停止時の実行中ジョブは`queued`へ戻り、次回起動時に再開します。ComfyUIへの投入順を保証するため、Uvicornは単一workerで起動してください。

## workflow・構図制御

「生成環境・ロケーション」からworkflowプリセットを作成し、checkpoint、VAE、sampler、scheduler、steps、CFG、denoiseを指定ノードへ反映できます。空欄の項目は元workflowの設定を維持します。

ロケーションには背景prompt、negative、参照画像を保存できます。各コマにはpose、depth、lineart、background参照画像と`LoadImage`ノードIDを指定できます。ControlNet等のモデル・強度・配線はComfyUI workflow側で設定します。

吹き出しはコマ画像上でドラッグ・リサイズでき、crop基準も方向ボタンで変更できます。コマ一覧は未完成コマだけに絞り込めます。

## 作品知識DBと長編ネーム生成（ローカルLLM）

4ページ複製方式に代えて、ローカルLLMで企画・全体プロット・ページ構成・コマ台本を段階生成できます。LLMがなくてもstubで全工程を確認できます。UIは`制作`・`作品知識`・`ストーリー生成`タブに分かれています。

### LLM設定

OpenAI互換API（LM Studio、llama.cpp、Ollama互換エンドポイントなど）を外部プロセスとして起動し、次を設定します。本アプリにLLMは同梱しません。

```powershell
$env:LLM_PROVIDER = "openai_compatible"   # stub | openai_compatible
$env:LLM_BASE_URL = "http://127.0.0.1:1234/v1"
$env:LLM_MODEL = "your-model-id"
$env:LLM_TIMEOUT_SECONDS = "180"
$env:LLM_JSON_MODE = "auto"               # auto | response_format | prompt_only
$env:LLM_MAX_CONTEXT_CHARS = "24000"
```

`GET /api/llm/status`で接続状態とモデル一覧を確認できます。

### 作品知識DB

作品名単位で全プロジェクトから知識を再利用します。`作品知識`タブからJSON・Markdown・TXTを取り込み、各文書に`required`（必須条件）または`reference`（参考情報）を設定します。

通常は`data/knowledge/<work_id>/`へローカル知識パックを配置します。`work.json`に作品名とJSON文書を列挙すると、`ストーリー生成`タブの作品知識選択肢へ自動表示されます。選択して新規セッションを作成した時点でDBへ同期されるため、ファイル修正後も再登録は不要です。形式は`data/knowledge/README.md`を参照してください。

知識ディレクトリを変更する場合は`KNOWLEDGE_DIR`を設定します。

```powershell
$env:KNOWLEDGE_DIR = "data/knowledge"
```

- JSONは`kind/title/content/policy/tags`形式で1件ずつ分割します
- Markdownは見出し単位で分割します
- TXTは文字数単位で分割します

検索はSQLite FTS5のtrigram索引を使い、3文字以上はtrigram検索、短いキャラ名等は`LIKE`検索で補完します（FTS5が使えない環境では`LIKE`へ退避します）。

### 段階生成

`ストーリー生成`タブで次の順に生成します。前段階を承認するまで次段階は生成できません。上流段階を編集すると下流段階は未承認へ戻ります。

1. 企画: あらすじ、トーン、キャラの役割、原作準拠条件
2. 全体プロット: 起承転結、主要ビート、キャラアーク
3. ページ構成: 指定ページ数分の目的、場面、登場人物、引き
4. コマ台本: shot、camera、location、visual prompt、台詞、効果音

LLMには`required`知識を必須条件、`reference`知識を参考情報として渡します。Pydantic検証に失敗した場合は、エラー内容を添えて1回だけ修正を要求します。各段階はフォームと折りたたみJSONの両方で編集できます。

### プロジェクトへの適用とリビジョン

台本を承認すると`プロジェクトへ適用`できます。コマのbboxはLLMには作らせず、サーバーがコマ数とlayout hintから既存レイアウトを割り当てます。1～4コマ／ページを許可し、ページ数は4・8・16と一致させます。キャラ、ロケーション、workflow、共通prompt設定は既存プロジェクトから維持し、画像候補とレンダリング状態は初期化します。

適用前のManga JSONは自動でリビジョン保存され、いつでも復元できます。復元時も現在状態を新しいリビジョンとして保存するため、往復できます。

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

### Anima 3既定プロンプト

同梱workflowの`anima-preview3-base.safetensors`設定に合わせ、共通positiveは`masterpiece, best quality, score_7, safe, anime`、共通negativeは`worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia`を基準にしています。漫画制作向けに手・人体の破綻と文字・透かし・吹き出しの抑制語も追加しています。制作画面の`共通プロンプト`から編集できます。

### 制作画面

- 左上のメニューボタンでプロジェクト一覧を開閉します
- `+`からタイトルだけを入力して本を作成し、作品知識とページ数は`ストーリー生成`で設定します
- `全ページ生成`は全コマをキューへ登録し、完了後に全ページをレンダリングします
- 生成中の進捗は画面下部へ固定表示され、許可済みの場合は完了時にデスクトップ通知を表示します
- Manga JSON、キャラクター、生成環境は折り畳み内で編集します

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
- `POST /api/projects/{id}/generation-jobs`
- `GET /api/projects/{id}/panels/{panel_id}/prompt-preview`
- `POST /api/projects/{id}/characters/{character_id}/reference-image`
- `POST /api/projects/{id}/locations/{location_id}/reference-image`
- `POST /api/projects/{id}/panels/{panel_id}/controls/{kind}/reference-image`
- `POST /api/projects/{id}/pages/{page}/overlays/{overlay_id}/{asset|mask}`
- `POST /api/projects/{id}/preflight`
- `POST /api/projects/{id}/pages/{page}/preflight`
- `GET /api/fonts`
- `GET /api/fonts/dialogue/file`
- `GET /api/projects/{id}/production-status`
- `GET /api/projects/{id}/generation-jobs`
- `GET /api/generation-jobs/{job_id}`
- `POST /api/generation-jobs/{job_id}/cancel`
- `WS /api/generation-jobs/{job_id}/ws`
- `POST /api/projects/{id}/panels/{panel_id}/candidates/{candidate_id}/select`
- `POST /api/projects/{id}/panels/{panel_id}/render-page`
- `POST /api/projects/{id}/export/cbz`
- `GET /api/assets/{asset_id}`
- `GET /api/llm/status`
- `POST /api/knowledge/sources/import`
- `POST /api/knowledge/documents`
- `GET /api/knowledge/sources?work_name=...`
- `DELETE /api/knowledge/sources/{id}`
- `POST /api/knowledge/search`
- `POST /api/projects/{id}/story-sessions`
- `GET /api/projects/{id}/story-sessions`
- `GET /api/story-sessions/{id}`
- `POST /api/story-sessions/{id}/stages/{brief|plot|pages|script}/generate`
- `PUT /api/story-sessions/{id}/stages/{stage}`
- `POST /api/story-sessions/{id}/stages/{stage}/approve`
- `POST /api/story-sessions/{id}/apply`
- `GET /api/projects/{id}/revisions`
- `POST /api/projects/{id}/revisions/{revision_id}/restore`

## ページ編集とoverlay

`ページ編集`では、コマの移動・拡縮、crop、吹き出し、しっぽ、SFXに加えてオーバーフレームを編集できます。`オーバーフレーム`から要素を追加し、透過画像またはマスク、抽出元コマ、透明度、倍率、前後レイヤー、z-index、手前に戻すコマを設定してください。保存時はManga JSON保存後にサーバーで対象ページを再レンダリングし、その画像を正規プレビューとして更新します。

アセットは`EXPORT_DIR`からの相対POSIX IDでManga JSONへ保存します。旧形式の絶対パスは読込時に正規化されます。`/api/assets`は`EXPORT_DIR`外への参照を拒否します。

## 品質検査

全品質ゲートはリポジトリルートから実行します。

```powershell
.\scripts\check.ps1
```

個別実行は次のとおりです。

```powershell
uv run ruff check backend tests
uv run ruff format --check backend tests
uv run mypy backend --no-sqlite-cache
uv run pytest --cov=backend

Set-Location frontend
npm run check
npm run test:e2e
```

Backend coverage下限は75%、現在の実測値は84%以上です。PRの変更Python行はGitHub Actionsで90%以上を要求します。Frontend coverageは60%から段階的に引き上げる設定で、初期JavaScript chunkは300KBを上限とします。OpenAPI定義は`scripts/export-openapi.py`と`npm run api:generate`で更新し、CIで生成差分を検査します。
