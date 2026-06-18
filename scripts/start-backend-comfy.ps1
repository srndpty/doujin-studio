param(
    [string]$ComfyBaseUrl = "http://127.0.0.1:8001",
    [string]$WorkflowPath = "workflows/default.workflow_api.json",
    [int]$Port = 8000
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

Write-Host "ComfyUI backend: $env:COMFYUI_BASE_URL"
Write-Host "Workflow: $env:COMFYUI_WORKFLOW_PATH"
Write-Host "API: http://127.0.0.1:$Port"

& .\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port $Port
