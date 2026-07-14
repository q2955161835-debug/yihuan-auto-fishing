param(
    [string]$TargetPath
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$Exe = $TargetPath
if ([string]::IsNullOrWhiteSpace($Exe)) {
    $Exe = Join-Path $PSScriptRoot '..\dist\异环自动钓鱼.exe'
}
$ExePath = (Resolve-Path -LiteralPath $Exe).Path

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
$IsAdministrator = $Principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $IsAdministrator) {
    throw '烟雾测试必须在管理员 PowerShell 中运行，防止无法关闭 requireAdministrator 发布物并遗留进程'
}

$Launcher = Start-Process -FilePath $ExePath -PassThru
$OwnedProcessIds = [System.Collections.Generic.HashSet[int]]::new()
$null = $OwnedProcessIds.Add([int]$Launcher.Id)

function Update-OwnedProcessIds {
    $Snapshot = @(Get-CimInstance Win32_Process)
    do {
        $Changed = $false
        foreach ($Item in $Snapshot) {
            if ($OwnedProcessIds.Contains([int]$Item.ParentProcessId)) {
                if ($OwnedProcessIds.Add([int]$Item.ProcessId)) {
                    $Changed = $true
                }
            }
        }
    } while ($Changed)
}

function Get-OwnedProcesses {
    Update-OwnedProcessIds
    @(
        foreach ($OwnedId in $OwnedProcessIds) {
            $Process = Get-Process -Id $OwnedId -ErrorAction SilentlyContinue
            if ($null -ne $Process) {
                $Process
            }
        }
    )
}

try {
    $StartupDeadline = [DateTime]::UtcNow.AddSeconds(15)
    $ResponsiveWindows = @()
    do {
        Start-Sleep -Milliseconds 200
        $Launcher.Refresh()
        $Running = Get-OwnedProcesses
        if ($Running.Count -eq 0) {
            if ($Launcher.HasExited) {
                throw "发布物提前退出，退出码 $($Launcher.ExitCode)"
            }
            continue
        }

        $ResponsiveWindows = @(
            foreach ($Item in $Running) {
                $Process = Get-Process -Id $Item.Id -ErrorAction SilentlyContinue
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
    } while (
        $ResponsiveWindows.Count -eq 0 -and
        [DateTime]::UtcNow -lt $StartupDeadline
    )
    if ($ResponsiveWindows.Count -eq 0) {
        throw '发布物窗口无响应'
    }
} finally {
    $Remaining = Get-OwnedProcesses
    foreach ($Process in $Remaining) {
        if ($Process.MainWindowHandle -ne 0) {
            $null = $Process.CloseMainWindow()
        }
    }

    $Deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 100
        $Remaining = Get-OwnedProcesses
    } while ($Remaining.Count -ne 0 -and [DateTime]::UtcNow -lt $Deadline)

    if ($Remaining.Count -ne 0) {
        Stop-Process -Id $Remaining.Id -Force -ErrorAction SilentlyContinue
        $ForceStopDeadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            Start-Sleep -Milliseconds 100
            $Remaining = Get-OwnedProcesses
        } while (
            $Remaining.Count -ne 0 -and
            [DateTime]::UtcNow -lt $ForceStopDeadline
        )
    }
    if ($Remaining.Count -ne 0) {
        throw "发布物仍有残留进程：$($Remaining.Id -join ', ')"
    }
}

'SMOKE_OK'
