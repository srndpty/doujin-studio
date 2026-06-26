<#
.SYNOPSIS
  ComfyUI を検出（または headless 起動）し、backend と frontend をまとめて起動する開発用ランチャ。
  LLM は VRAM 都合で別運用のため、このスクリプトでは起動しない。

.EXAMPLE
  # 既に動いている ComfyUI(Desktop等)を自動検出して backend+frontend を起動
  .\scripts\dev-up.ps1

.EXAMPLE
  # ソース版 ComfyUI を Desktop の basePath を再利用して headless 起動してから一括起動
  .\scripts\dev-up.ps1 -ComfyPath C:\tools\ComfyUI -ComfyBasePath "C:\Users\<you>\Documents\ComfyUI"
#>
param(
    [string]$ComfyBaseUrl = "",                 # 既知の ComfyUI URL。指定時は検出/起動をスキップ
    [int[]]$ProbePorts = @(8188, 8001, 8000),   # ComfyUI 自動検出の候補ポート(127.0.0.1)
    [string]$ComfyPath = "",                    # headless ソース版を起動する場合のフォルダ
    [string]$ComfyBasePath = "",                # headless 時に再利用する Desktop basePath
    [int]$BackendPort = 8000,
    [int]$WaitSeconds = 90,                     # ComfyUI 起動待ちの上限
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

function Test-Comfy([string]$url) {
    try {
        return (Invoke-WebRequest -Uri "$url/system_stats" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200
    } catch {
        return $false
    }
}

function Find-Comfy([int[]]$ports) {
    foreach ($port in $ports) {
        $url = "http://127.0.0.1:$port"
        if (Test-Comfy $url) { return $url }
    }
    return ""
}

# 別ウィンドウで PowerShell コマンドを起動するヘルパー（ログを各ウィンドウに残す）。
$shell = $null
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) { $shell = $pwsh.Source } else { $shell = (Get-Command powershell).Source }
function Start-Window([string]$title, [string]$command) {
    Start-Process -FilePath $shell -ArgumentList @(
        "-NoExit", "-Command",
        "`$Host.UI.RawUI.WindowTitle='$title'; Set-Location '$root'; $command"
    )
}

# 1) ComfyUI: 既知URL > 検出 > headless起動
if (-not $ComfyBaseUrl) { $ComfyBaseUrl = Find-Comfy $ProbePorts }
if (-not $ComfyBaseUrl -and $ComfyPath) {
    $comfyArgs = @("-ComfyPath", $ComfyPath, "-Port", $ProbePorts[0])
    if ($ComfyBasePath) { $comfyArgs += @("-BasePath", $ComfyBasePath) }
    Start-Window "ComfyUI" ("& .\scripts\start-comfy-headless.ps1 " + ($comfyArgs -join ' '))
    $url = "http://127.0.0.1:$($ProbePorts[0])"
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline -and -not (Test-Comfy $url)) { Start-Sleep -Seconds 2 }
    if (Test-Comfy $url) { $ComfyBaseUrl = $url }
}

if ($ComfyBaseUrl) {
    Write-Host "ComfyUI: $ComfyBaseUrl (connected)"
} else {
    Write-Warning "ComfyUI が見つかりません。Desktop アプリを起動するか -ComfyPath を指定してください。backend は未接続で起動します。"
    $ComfyBaseUrl = "http://127.0.0.1:$($ProbePorts[0])"
}

# 2) backend
Start-Window "backend" ("& .\scripts\start-backend-comfy.ps1 -ComfyBaseUrl '$ComfyBaseUrl' -Port $BackendPort")

# 3) frontend
if (-not $NoFrontend) {
    Start-Window "frontend" ("Set-Location frontend; npm run dev")
}

Write-Host ""
Write-Host "起動しました:"
Write-Host "  ComfyUI : $ComfyBaseUrl"
Write-Host "  backend : http://127.0.0.1:$BackendPort  (確認: /api/comfyui/status)"
if (-not $NoFrontend) { Write-Host "  frontend: http://127.0.0.1:5173" }
Write-Host "  LLM     : 別途起動してください（例: LM Studio :1234 / Ollama）"
