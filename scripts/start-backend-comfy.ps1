param(
    [string]$ComfyBaseUrl = "http://127.0.0.1:8188",
    [string]$WorkflowPath = "workflows/default.workflow_api.json",
    [int]$Port = 8000,
    # 既定はコード変更を自動反映する開発モード。本番的に起動するなら -NoReload を付ける。
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"

Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

$env:IMAGE_BACKEND = "comfyui"
$env:COMFYUI_BASE_URL = $ComfyBaseUrl
$env:COMFYUI_WORKFLOW_PATH = $WorkflowPath
$env:COMFYUI_POSITIVE_NODE_ID = "11"
$env:COMFYUI_NEGATIVE_NODE_ID = "12"
$env:COMFYUI_SEED_NODE_ID = "19"
$env:COMFYUI_WIDTH_NODE_ID = "28"
$env:COMFYUI_HEIGHT_NODE_ID = "28"
$env:COMFYUI_SAVE_PREFIX_NODE_ID = "46"
$env:COMFYUI_TIMEOUT_SECONDS = "180"

$uvicornArgs = @("backend.app.main:app", "--host", "127.0.0.1", "--port", "$Port")
if (-not $NoReload) {
    # backend配下のコード変更だけ監視する（.venv/data/exportsを監視すると重く誤検知も増えるため）。
    $uvicornArgs += @("--reload", "--reload-dir", "backend")
}

# 依存とvenvをuvに統一する。uvが無い環境では従来の.venv直起動へフォールバックする。
$uv = Get-Command uv -ErrorAction SilentlyContinue

Write-Host "ComfyUI backend: $env:COMFYUI_BASE_URL"
Write-Host "Workflow: $env:COMFYUI_WORKFLOW_PATH"
Write-Host "API: http://127.0.0.1:$Port"
Write-Host "Reload: $([bool](-not $NoReload)) / Runner: $(if ($uv) { 'uv' } else { '.venv' })"

if ($uv) {
    & uv run uvicorn @uvicornArgs
} else {
    & .\.venv\Scripts\python.exe -m uvicorn @uvicornArgs
}
