param(
    [string]$PythonPath = $env:AUTO_FISHING_PYTHON
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $Root '.venv\Scripts\python.exe'
} elseif (-not [System.IO.Path]::IsPathRooted($PythonPath)) {
    $PythonPath = Join-Path $Root $PythonPath
}
$Python = $PythonPath

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到 Python 解释器：$Python"
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
