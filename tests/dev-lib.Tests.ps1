# scripts/dev-lib.ps1 の純粋ヘルパーのテスト（Pester）。
# 実行: Invoke-Pester -Path tests/dev-lib.Tests.ps1

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "..\scripts\dev-lib.ps1")

Describe "ConvertTo-PSLiteral" {
    It "単一引用符で囲む" {
        ConvertTo-PSLiteral "C:\tools\ComfyUI" | Should Be "'C:\tools\ComfyUI'"
    }
    It "内部の単一引用符を '' へエスケープする" {
        ConvertTo-PSLiteral "C:\Users\o'brien\ComfyUI" | Should Be "'C:\Users\o''brien\ComfyUI'"
    }
}

Describe "Select-AvailablePort" {
    It "空いている最初のポートを返す" {
        Select-AvailablePort @(8188, 8001, 8000) { param($p) $p -eq 8001 } | Should Be 8001
    }
    It "占有ポートはスキップする" {
        # 8188 占有・8001 占有 → 8000 を選ぶ
        Select-AvailablePort @(8188, 8001, 8000) { param($p) $p -eq 8000 } | Should Be 8000
    }
    It "空きが無ければ例外" {
        $threw = $false
        try { Select-AvailablePort @(8188, 8001) { param($p) $false } } catch { $threw = $true }
        $threw | Should Be $true
    }
}

Describe "New-WaitForComfyCommand" {
    $cmd = New-WaitForComfyCommand "http://127.0.0.1:8188/system_stats"
    It "成功を ready フラグで明示保持する" {
        $cmd | Should Match '\$ready=\$false'
        $cmd | Should Match '\$ready=\$true'
    }
    It "警告は ready でないときだけ出す（期限直前成功で誤警告しない）" {
        $cmd | Should Match 'if \(-not \$ready\)'
        $cmd | Should Not Match '\(Get-Date\) -ge'
    }
    It "status URL を PS リテラルとして埋め込む" {
        $cmd | Should Match "Invoke-WebRequest -Uri 'http://127.0.0.1:8188/system_stats'"
    }
}
