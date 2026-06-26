<#
.SYNOPSIS
  ステージ切替時に GPU VRAM を解放するヘルパー。プロセスは落とさず、モデルだけ退避する。
  - ComfyUI: /free で VRAM を解放（サーバは起動したまま）。
  - Ollama: 指定モデルを ollama stop で即アンロード（任意）。
  LM Studio を使う場合は、モデルの TTL / JIT auto-evict を短く設定すると自動で退避する。

.EXAMPLE
  # 台本(LLM)生成の直前に ComfyUI の VRAM を空ける
  .\scripts\gpu-free.ps1 -ComfyBaseUrl http://127.0.0.1:8001

.EXAMPLE
  # 画像生成の直前に Ollama のモデルを退避
  .\scripts\gpu-free.ps1 -OllamaModel qwen2.5:14b
#>
param(
    [string]$ComfyBaseUrl = "http://127.0.0.1:8188",
    [string]$OllamaModel = ""
)

# ComfyUI の VRAM 解放
try {
    $body = @{ unload_models = $true; free_memory = $true } | ConvertTo-Json
    Invoke-RestMethod -Uri "$ComfyBaseUrl/free" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 10 | Out-Null
    Write-Host "ComfyUI: VRAM 解放を要求しました ($ComfyBaseUrl/free)"
} catch {
    Write-Warning "ComfyUI /free 失敗: $($_.Exception.Message)"
}

# Ollama のモデルをアンロード（任意）
if ($OllamaModel) {
    try {
        & ollama stop $OllamaModel
        Write-Host "Ollama: $OllamaModel をアンロードしました"
    } catch {
        Write-Warning "ollama stop 失敗: $($_.Exception.Message)"
    }
}
