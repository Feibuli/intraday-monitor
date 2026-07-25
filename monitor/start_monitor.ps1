<#
.SYNOPSIS
    Stock Monitor Launcher - creates weekday 9:15 wake task, then starts monitor.
    Run as administrator for the wake task feature.
#>

$taskName = "StockMonitorWake"
# Python 解释器路径解析顺序：
#   1) 环境变量 WB_PYTHON（显式覆盖，最高优先级）
#   2) WorkBuddy 内置 Python（位于 $env:USERPROFILE\.workbuddy\binaries\python\versions\<最新版本>\python.exe，已含纯标准库运行所需环境，开箱即用）
#   3) PATH 中的 python / python3
#   4) 以上都没有 → 给出可读错误
# 说明：监控脚本为纯 Python 标准库实现，无需 pip 安装任何第三方依赖；只要有 Python 3 即可开箱即用。
if ($env:WB_PYTHON) {
    $pythonExe = $env:WB_PYTHON
} else {
    $pythonExe = $null
    $wbRoot = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
    if (Test-Path $wbRoot) {
        $wbPy = Get-ChildItem $wbRoot -Directory | Sort-Object Name | Select-Object -Last 1 |
                ForEach-Object { Join-Path $_.FullName "python.exe" }
        if ($wbPy -and (Test-Path $wbPy)) { $pythonExe = $wbPy }
    }
    if (-not $pythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $pythonExe = "python"
    }
    if (-not $pythonExe -and (Get-Command python3 -ErrorAction SilentlyContinue)) {
        $pythonExe = "python3"
    }
    if (-not $pythonExe) {
        Write-Error "未找到 Python。请安装 Python 并加入 PATH，或设置环境变量 WB_PYTHON 指向 python.exe（如 WorkBuddy 内置 python）。"
        exit 1
    }
}
$workDir = $PSScriptRoot

Write-Host "============================================"
Write-Host "  Stock Monitor Launcher (with 9:15 wake)"
Write-Host "============================================"
Write-Host ""

# 说明：监控脚本为纯 Python 标准库实现，无需 pip 安装任何第三方依赖，只要有 Python 3 即可运行。

# Step 1: Register weekday 9:15 wake task（尽力而为：未以管理员身份运行或注册失败都不影响监控启动）
Write-Host "[1/2] Registering weekday 9:15 wake task..."
try {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Write-Host "  Wake task already exists, skipping"
} catch {
    try {
        $action = New-ScheduledTaskAction -Execute $pythonExe -Argument "stock_monitor.py" -WorkingDirectory $workDir
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:15"
        $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force -ErrorAction Stop
        Write-Host "  Wake task created!"
    } catch {
        Write-Warning "  未能创建唤醒任务（需要管理员权限）。不影响监控运行，可忽略；如需交易日自动唤醒，请用管理员身份运行本脚本。"
    }
}

# Step 2: Start monitor
Write-Host ""
Write-Host "[2/2] Starting stock monitor..."
Set-Location $workDir
& $pythonExe "$workDir\stock_monitor.py"
$monitorExit = $LASTEXITCODE

Write-Host ""
Write-Host "Monitor stopped."
if ($monitorExit -ne 0) {
    Write-Warning "监控进程异常退出（退出码 $monitorExit）。常见原因：未找到可用的 Python，或 WB_PYTHON 指向的 Python 不正确。请确认本机已安装 Python 3，或设置环境变量 WB_PYTHON 指向 python.exe。"
}
Read-Host "Press Enter to exit"
