$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw '缺少 .venv，请先建立 Python 3.13 虚拟环境'
}

Push-Location $Root
try {
    & $Python -m pytest (Join-Path $Root 'try\tests') -q
    if ($LASTEXITCODE -ne 0) {
        throw '自动测试失败，停止构建'
    }

    & $Python -m PyInstaller --clean --noconfirm (Join-Path $Root 'packaging\auto_fishing.spec')
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller 构建失败'
    }

    $Exe = Join-Path $Root 'dist\异环自动钓鱼.exe'
    if (-not (Test-Path -LiteralPath $Exe)) {
        throw '未生成预期发布物'
    }

    Get-FileHash -Algorithm SHA256 -LiteralPath $Exe
} finally {
    Pop-Location
}
