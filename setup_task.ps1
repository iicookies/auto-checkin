# 注册 / 卸载 Windows 任务计划，每天定时执行签到。
#
# 用法（以管理员身份运行 PowerShell）:
#   .\setup_task.ps1 -Install          # 注册每天 09:05 执行的任务
#   .\setup_task.ps1 -Time 08:30       # 注册并指定时间
#   .\setup_task.ps1 -Uninstall       # 卸载任务
#   .\setup_task.ps1 -RunNow          # 立即执行一次签到
#   .\setup_task.ps1 -Status          # 查看任务状态
#
# 任务名: AutoCheckin
# 日志:   logs\checkin.log

param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$RunNow,
    [switch]$Status,
    [string]$Time = "09:05"
)

$ErrorActionPreference = "Stop"
$TaskName = "AutoCheckin"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = (Get-Command python).Source
$CheckinScript = Join-Path $ScriptDir "checkin.py"
$LogDir = Join-Path $ScriptDir "logs"
$LogFile = Join-Path $LogDir "checkin.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# 确认 python 可用
if (-not $Python) {
    Write-Host "未找到 python，请先安装 Python 并加入 PATH。" -ForegroundColor Red
    exit 1
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "任务 '$TaskName' 不存在。" -ForegroundColor Yellow
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "任务名:       $TaskName"
    Write-Host "状态:         $($task.State)"
    Write-Host "触发器:       每天 $($task.Triggers[0].StartBoundary.Substring(11,5))"
    Write-Host "上次运行:     $($info.LastRunTime)"
    Write-Host "上次结果:     $($info.LastTaskResult)"
    Write-Host "下次运行:     $($info.NextRunTime)"
    Write-Host "脚本:         $CheckinScript"
    Write-Host "日志:         $LogFile"
    exit 0
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "任务 '$TaskName' 不存在，无需卸载。" -ForegroundColor Yellow
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已卸载任务 '$TaskName'。" -ForegroundColor Green
    exit 0
}

if ($RunNow) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "任务 '$TaskName' 不存在，请先 -Install。" -ForegroundColor Red
        exit 1
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "已触发立即执行，查看日志: $LogFile" -ForegroundColor Green
    exit 0
}

if ($Install) {
    # 校验时间格式
    $t = Get-Date $Time -ErrorAction Stop

    # 构造动作：python checkin.py
    # 日志由 checkin.py 内部的 logging.FileHandler 写入 logs\checkin.log，不需要 shell 重定向
    $action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument "`"$CheckinScript`"" `
        -WorkingDirectory $ScriptDir

    # 每天定时触发
    $trigger = New-ScheduledTaskTrigger -Daily -At $t

    # 允许在电池供电时也运行，失败后重试
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 15)

    # 用当前用户身份运行（交互式，可访问用户级凭证）
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    # 若已存在先卸载
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除旧任务。" -ForegroundColor Yellow
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "WorkBuddy + Trae 每日自动签到" | Out-Null

    Write-Host "已注册任务 '$TaskName'，每天 $Time 执行。" -ForegroundColor Green
    Write-Host "脚本:   $CheckinScript"
    Write-Host "日志:   $LogFile"
    Write-Host ""
    Write-Host "常用操作:"
    Write-Host "  立即执行一次: .\setup_task.ps1 -RunNow"
    Write-Host "  查看状态:     .\setup_task.ps1 -Status"
    Write-Host "  卸载:         .\setup_task.ps1 -Uninstall"
    exit 0
}

Write-Host "请指定操作: -Install / -Uninstall / -RunNow / -Status"
Write-Host "示例: .\setup_task.ps1 -Install -Time 08:30"
exit 0
