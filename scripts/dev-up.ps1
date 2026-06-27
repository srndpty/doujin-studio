<#
.SYNOPSIS
  ComfyUI を検出（または headless 起動）し、backend と frontend を起動する開発用ランチャ。
  Windows Terminal がある場合は 1 ウィンドウの複数タブにまとめて開く（手動の連結が不要）。
  LLM は VRAM 都合で別運用が既定。-Ollama で Ollama も自動管理する。

.EXAMPLE
  # 既に動いている ComfyUI(Desktop等)を検出して backend+frontend を 1 ウィンドウのタブで起動
  .\scripts\dev-up.ps1

.EXAMPLE
  # ソース版 ComfyUI を headless 起動し、Ollama も含めて全部タブで起動
  .\scripts\dev-up.ps1 -ComfyPath C:\tools\ComfyUI -ComfyBasePath "C:\Users\<you>\Documents\ComfyUI" -Ollama
#>
param(
    [string]$ComfyBaseUrl = "",                 # 既知の ComfyUI URL。指定時は検出/起動をスキップ
    [int[]]$ProbePorts = @(8188, 8001, 8000),   # ComfyUI 自動検出の候補ポート(127.0.0.1)
    [string]$ComfyPath = "",                    # headless ソース版を起動する場合のフォルダ
    [string]$ComfyBasePath = "",                # headless 時に再利用する Desktop basePath
    [int]$BackendPort = 8000,
    # Ollama を一緒に起動し、LLM も自動管理する（VRAM は keep_alive=0 で各生成後に自動退避）。
    [switch]$Ollama,
    [string]$OllamaModel = "qwen3.6:27b",
    [int]$OllamaPort = 11434,
    [switch]$NoFrontend,
    # Windows Terminal のタブにまとめず、従来どおり各プロセスを別ウィンドウで開く。
    [switch]$SeparateWindows
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

# 起動シェル（pwsh 優先、無ければ Windows PowerShell）。
$shell = $null
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) { $shell = $pwsh.Source } else { $shell = (Get-Command powershell).Source }

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

# 収集したタブ（各 @{Title; Command}）を Windows Terminal の 1 ウィンドウに開く。
# wt が無い／-SeparateWindows 指定時は従来どおり別ウィンドウで開く。
function Open-Tabs($tabs) {
    $wt = Get-Command wt -ErrorAction SilentlyContinue
    if ($wt -and -not $SeparateWindows) {
        $wtArgs = @()
        for ($i = 0; $i -lt $tabs.Count; $i++) {
            if ($i -gt 0) { $wtArgs += ";" }   # wt のタブ区切り
            # タブ内コマンドの ; や引用符を wt/シェルに誤解析させないため EncodedCommand で渡す。
            $full = "Set-Location '$root'; " + $tabs[$i].Command
            $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($full))
            $wtArgs += @("new-tab", "--title", $tabs[$i].Title, $shell, "-NoExit", "-EncodedCommand", $b64)
        }
        & $wt.Source @wtArgs
        Write-Host "Windows Terminal の 1 ウィンドウに $($tabs.Count) タブで起動しました。"
    } else {
        foreach ($tab in $tabs) {
            Start-Process -FilePath $shell -ArgumentList @(
                "-NoExit", "-Command",
                "`$Host.UI.RawUI.WindowTitle='$($tab.Title)'; Set-Location '$root'; $($tab.Command)"
            )
        }
        Write-Host "各プロセスを別ウィンドウで起動しました（wt が無い、または -SeparateWindows）。"
    }
}

$tabs = New-Object System.Collections.Generic.List[object]

# 1) ComfyUI: 既知URL > 検出 > headless起動（タブ追加）
if (-not $ComfyBaseUrl) { $ComfyBaseUrl = Find-Comfy $ProbePorts }
if (-not $ComfyBaseUrl -and $ComfyPath) {
    $port = $ProbePorts[0]
    $cmd = "& .\scripts\start-comfy-headless.ps1 -ComfyPath '$ComfyPath' -Port $port"
    if ($ComfyBasePath) { $cmd += " -BasePath '$ComfyBasePath'" }
    $tabs.Add(@{ Title = "ComfyUI"; Command = $cmd })
    $ComfyBaseUrl = "http://127.0.0.1:$port"
    Write-Host "ComfyUI: $ComfyBaseUrl (headless 起動)"
} elseif ($ComfyBaseUrl) {
    Write-Host "ComfyUI: $ComfyBaseUrl (connected)"
} else {
    Write-Warning "ComfyUI が見つかりません。Desktop アプリを起動するか -ComfyPath を指定してください。backend は未接続で起動します。"
    $ComfyBaseUrl = "http://127.0.0.1:$($ProbePorts[0])"
}

# 2) Ollama（任意）: serve を keep_alive=0 で起動。backend へ LLM_* と /free フラグを渡す。
$backendEnvPrefix = ""
if ($Ollama) {
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        $ollamaUrl = "http://127.0.0.1:$OllamaPort"
        # API で起動確認とモデル一覧を取得する。`ollama list`(CLI)はサーバを同期起動して
        # ブロックし得るため使わない（タブを開く前に固まる原因になる）。
        $models = $null
        try { $models = (Invoke-RestMethod -Uri "$ollamaUrl/api/tags" -TimeoutSec 3).models } catch { $models = $null }
        if ($null -ne $models) {
            Write-Warning "既存の Ollama を使用します。keep_alive=0 を効かせるには 'setx OLLAMA_KEEP_ALIVE 0' 後にアプリ再起動してください。"
            if (-not ($models.name -contains $OllamaModel)) {
                Write-Warning "モデル '$OllamaModel' が未取得です。'ollama pull $OllamaModel' を実行してください（実在タグは ollama.com/library で確認）。"
            }
        } else {
            # サーバ未起動 → serve をタブで起動（モデル確認は起動後に各自）。
            $tabs.Add(@{ Title = "ollama"; Command = "`$env:OLLAMA_KEEP_ALIVE='0'; ollama serve" })
            Write-Host "Ollama: serve をタブで起動します（未取得なら 'ollama pull $OllamaModel'）。"
        }
        $backendEnvPrefix = "`$env:LLM_PROVIDER='openai_compatible'; `$env:LLM_BASE_URL='$ollamaUrl/v1'; `$env:LLM_MODEL='$OllamaModel'; `$env:COMFYUI_FREE_BEFORE_LLM='1'; "
    } else {
        Write-Warning "ollama が見つかりません。https://ollama.com からインストールしてください。"
    }
}

# 3) backend
$tabs.Add(@{
        Title   = "backend"
        Command = ($backendEnvPrefix + "& .\scripts\start-backend-comfy.ps1 -ComfyBaseUrl '$ComfyBaseUrl' -Port $BackendPort")
    })

# 4) frontend
if (-not $NoFrontend) {
    $tabs.Add(@{ Title = "frontend"; Command = "Set-Location frontend; npm run dev" })
}

Open-Tabs $tabs

Write-Host ""
Write-Host "起動内容:"
Write-Host "  ComfyUI : $ComfyBaseUrl"
Write-Host "  backend : http://127.0.0.1:$BackendPort  (確認: /api/comfyui/status, /api/llm/status)"
if (-not $NoFrontend) { Write-Host "  frontend: http://127.0.0.1:5173" }
if ($Ollama) {
    Write-Host "  LLM     : Ollama http://127.0.0.1:$OllamaPort (model=$OllamaModel)"
    Write-Host "            台本生成前に ComfyUI を自動 /free（COMFYUI_FREE_BEFORE_LLM=1）→ VRAM 同居なし"
} else {
    Write-Host "  LLM     : 別途起動（-Ollama で自動管理。例: LM Studio :1234）"
}
