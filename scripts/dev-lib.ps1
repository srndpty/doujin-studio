<#
.SYNOPSIS
  dev-up.ps1 の純粋ヘルパー関数群。テスト可能にするため本体から分離している。
  この .ps1 は dot-source 用で、読み込んでも副作用（プロセス起動等）を起こさない。
#>

# パスやURLをコード文字列へ埋め込む際、単一引用符を '' にエスケープして安全な PS リテラルにする。
function ConvertTo-PSLiteral([string]$value) {
    "'" + $value.Replace("'", "''") + "'"
}

# 候補ポートから「空いている」最初のポートを返す。判定は $isAvailable に委譲（テスト用に注入可能）。
function Select-AvailablePort([int[]]$ports, [scriptblock]$isAvailable) {
    foreach ($port in $ports) {
        if (& $isAvailable $port) { return $port }
    }
    throw "空きポートがありません（候補: $($ports -join ', ')）。-ProbePorts を変更してください。"
}

# ポートが空いているか。Get-NetTCPConnection が使えない環境では TcpListener の bind で確認する。
# fallback の bind は $listen（ComfyUI の -Listen と一致させる）アドレスで試す。
# 注意: 確認と実際の bind の間には TOCTOU race が残る（別プロセスが先に取得しうる）。
# bind 失敗時は -ProbePorts に別候補を足して再実行する運用とする（docs参照）。
function Test-PortAvailable([int]$port, [string]$listen = "127.0.0.1") {
    try {
        return -not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        try {
            $address = switch ($listen) {
                "0.0.0.0" { [System.Net.IPAddress]::Any }
                "::" { [System.Net.IPAddress]::IPv6Any }
                default { [System.Net.IPAddress]::Parse($listen) }
            }
            $listener = [System.Net.Sockets.TcpListener]::new($address, $port)
            $listener.Start()
            $listener.Stop()
            return $true
        } catch {
            return $false
        }
    }
}

# ComfyUI(headless)の疎通を待ってから後続を実行する PS コマンド文字列を作る。
# 成功は $ready フラグで明示保持し、期限直前に成功しても誤って timeout 警告を出さない。
function New-WaitForComfyCommand([string]$statusUrl, [int]$timeoutMinutes = 3) {
    $uri = ConvertTo-PSLiteral $statusUrl
    return ("Write-Host 'ComfyUI の起動を待機中...'; " +
        "`$ready=`$false; `$deadline=(Get-Date).AddMinutes($timeoutMinutes); " +
        "do { try { if ((Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200){ `$ready=`$true; break } } catch {}; " +
        "Start-Sleep -Seconds 2 } while ((Get-Date) -lt `$deadline); " +
        "if (-not `$ready){ Write-Warning 'ComfyUI の起動待機がタイムアウトしました。' }; ")
}
