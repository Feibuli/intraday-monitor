<#
.SYNOPSIS
    Stock Monitor Launcher - creates weekday 9:15 wake task, then starts monitor.
    Run as administrator for the wake task feature.
#>

$taskName = "StockMonitorWake"
# Python 解释器路径：优先环境变量 WB_PYTHON，其次 PATH 中的 python/python3，最后给出可读错误。
if ($env:WB_PYTHON) {
    $pythonExe = $env:WB_PYTHON
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $pythonExe = "python3"
} else {
    Write-Error "未找到 Python。请安装 Python 并加入 PATH，或设置环境变量 WB_PYTHON 指向 python.exe（如 WorkBuddy 内置 python）。"
    exit 1
}
$workDir = $PSScriptRoot

Write-Host "============================================"
Write-Host "  Stock Monitor Launcher (with 9:15 wake)"
Write-Host "============================================"
Write-Host ""

# Step 1: Register weekday 9:15 wake task
Write-Host "[1/2] Registering weekday 9:15 wake task..."
try {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Write-Host "  Wake task already exists, skipping"
} catch {
    $action = New-ScheduledTaskAction -Execute $pythonExe -Argument "stock_monitor.py" -WorkingDirectory $workDir
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:15"
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force
    Write-Host "  Wake task created!"
}

# Step 2: Start monitor
Write-Host ""
Write-Host "[2/2] Starting stock monitor..."
Set-Location $workDir
& $pythonExe "$workDir\stock_monitor.py"

Write-Host ""
Write-Host "Monitor stopped."
Read-Host "Press Enter to exit"
