param(
    [Parameter(Mandatory = $true)]
    [string]$ComfyPath,
    [string]$Listen = "127.0.0.1",
    [int]$Port = 8188,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $ComfyPath).Path
$desktopMain = Join-Path $root "resources\ComfyUI\main.py"
$sourceMain = Join-Path $root "main.py"
$portableMain = Join-Path $root "ComfyUI\main.py"
$arguments = @()

if (Test-Path -LiteralPath $desktopMain -PathType Leaf) {
    # ComfyUI Desktopはプログラム本体とユーザーデータ・venvを別々に配置する。
    $configPath = Join-Path $env:APPDATA "ComfyUI\config.json"
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "ComfyUI Desktopの設定が見つかりません: $configPath"
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $basePath = [string]$config.basePath
    if (-not $basePath -or -not (Test-Path -LiteralPath $basePath -PathType Container)) {
        throw "ComfyUI DesktopのbasePathが不正です: $basePath"
    }
    $python = Join-Path $basePath ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "ComfyUI DesktopのPython環境が見つかりません: $python"
    }
    $main = $desktopMain
    $frontEndRoot = Join-Path $root "resources\ComfyUI\web_custom_versions\desktop_app"
    $extraModels = Join-Path $env:APPDATA "ComfyUI\extra_models_config.yaml"
    $arguments += @(
        "--user-directory", (Join-Path $basePath "user"),
        "--input-directory", (Join-Path $basePath "input"),
        "--output-directory", (Join-Path $basePath "output"),
        "--base-directory", $basePath,
        "--database-url", "sqlite:///$($basePath.Replace('\', '/'))/user/comfyui.db",
        "--enable-manager"
    )
    if (Test-Path -LiteralPath $frontEndRoot -PathType Container) {
        $arguments += @("--front-end-root", $frontEndRoot)
    }
    if (Test-Path -LiteralPath $extraModels -PathType Leaf) {
        $arguments += @("--extra-model-paths-config", $extraModels)
    }
    $workingDirectory = $basePath
} else {
    $main = if (Test-Path -LiteralPath $sourceMain -PathType Leaf) {
        $sourceMain
    } elseif (Test-Path -LiteralPath $portableMain -PathType Leaf) {
        $portableMain
    } else {
        throw "ComfyUIのmain.pyが見つかりません: $root"
    }
    $embedded = Join-Path $root "python_embeded\python.exe"
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    $python = if (Test-Path -LiteralPath $embedded -PathType Leaf) {
        $embedded
    } elseif (Test-Path -LiteralPath $venv -PathType Leaf) {
        $venv
    } else {
        (Get-Command python -ErrorAction Stop).Source
    }
    $workingDirectory = Split-Path -Parent $main
}

Write-Host "ComfyUIをheadless起動します: http://${Listen}:$Port"
Write-Host "Python: $python"
Write-Host "Main: $main"
Write-Host "Data: $workingDirectory"
if ($ValidateOnly) {
    Write-Host "検証のみ実行しました。ComfyUIは起動していません。"
    exit 0
}

Set-Location -LiteralPath $workingDirectory
& $python $main @arguments --listen $Listen --port $Port --disable-auto-launch
