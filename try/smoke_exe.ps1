$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$Exe = Resolve-Path (Join-Path $PSScriptRoot '..\dist\异环自动钓鱼.exe')
$ExePath = $Exe.Path

function Get-ExecutableProcesses {
    @(
        Get-CimInstance Win32_Process |
            Where-Object { $_.ExecutablePath -eq $ExePath }
    )
}

$Existing = Get-ExecutableProcesses
if ($Existing.Count -ne 0) {
    throw "烟雾开始前已有发布物进程：$($Existing.ProcessId -join ', ')"
}

$Launcher = Start-Process -FilePath $ExePath -PassThru

try {
    Start-Sleep -Seconds 3
    $Launcher.Refresh()
    $Running = Get-ExecutableProcesses
    if ($Running.Count -eq 0) {
        if ($Launcher.HasExited) {
            throw "发布物提前退出，退出码 $($Launcher.ExitCode)"
        }
        throw '未找到发布物进程'
    }

    $ResponsiveWindows = @(
        foreach ($Item in $Running) {
            $Process = Get-Process -Id $Item.ProcessId -ErrorAction SilentlyContinue
            if ($null -ne $Process) {
                $Process.Refresh()
                if (
                    -not $Process.HasExited -and
                    $Process.Responding -and
                    $Process.MainWindowHandle -ne 0
                ) {
                    $Process
                }
            }
        }
    )
    if ($ResponsiveWindows.Count -eq 0) {
        throw '发布物窗口无响应'
    }
} finally {
    $Remaining = Get-ExecutableProcesses
    foreach ($Item in $Remaining) {
        $Process = Get-Process -Id $Item.ProcessId -ErrorAction SilentlyContinue
        if ($null -ne $Process -and $Process.MainWindowHandle -ne 0) {
            $null = $Process.CloseMainWindow()
        }
    }

    $Deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 100
        $Remaining = Get-ExecutableProcesses
    } while ($Remaining.Count -ne 0 -and [DateTime]::UtcNow -lt $Deadline)

    if ($Remaining.Count -ne 0) {
        Stop-Process -Id $Remaining.ProcessId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 250
        $Remaining = Get-ExecutableProcesses
    }
    if ($Remaining.Count -ne 0) {
        throw "发布物仍有残留进程：$($Remaining.ProcessId -join ', ')"
    }
}

'SMOKE_OK'
