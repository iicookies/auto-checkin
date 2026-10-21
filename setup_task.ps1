# 注册 / 卸载 Windows 任务计划，每天定时执行签到。
#
# 用法（以管理员身份运行 PowerShell）:
#   .\setup_task.ps1 -Install          # 注册每天 10:05 执行的任务
#   .\setup_task.ps1 -Time 08:30       # 注册并指定时间
#   .\setup_task.ps1 -Install -ScriptArgs "--qoder-wait 15"   # 给脚本传额外参数
#   .\setup_task.ps1 -Uninstall       # 卸载任务
#   .\setup_task.ps1 -RunNow          # 立即执行一次签到
#   .\setup_task.ps1 -Status          # 查看任务状态
#
# 默认 10:05：Qoder 每日额度在 10:00（UTC+8）刷新，早于该点会提示"本轮活动未下发"
#
# 任务名: AutoCheckin
# 日志:   logs\checkin.log

param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$RunNow,
    [switch]$Status,
    [string]$Time = "10:05",
    [string]$ScriptArgs = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "AutoCheckin"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CheckinScript = Join-Path $ScriptDir "checkin.py"
$LogDir = Join-Path $ScriptDir "logs"
$LogFile = Join-Path $LogDir "checkin.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# 解析真实 python.exe，避开 PyManager 启动器（计划任务下易异常退出 0xC000013A）
function Resolve-PythonExe {
    $candidates = @()

    # 1) py launcher 指向的真实解释器
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $fromPy = & $py.Source -0p 2>$null | Select-Object -First 1
            if ($fromPy -match '^\s*-\S+\s+(.+\.exe)\s*$') {
                $candidates += $Matches[1].Trim()
            } elseif ($fromPy -and (Test-Path $fromPy.Trim())) {
                $candidates += $fromPy.Trim()
            }
        } catch {}
    }

    # 2) PATH 里的 python，但跳过 PyManager / WindowsApps 占位
    $pathPythons = @(Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($p in $pathPythons) {
        if (-not $p) { continue }
        if ($p -match '(?i)\\PyManager\\|\\WindowsApps\\') { continue }
        $candidates += $p
    }

    # 3) 常见安装位置兜底
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    )

    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }
    return $null
}

$Python = Resolve-PythonExe
if (-not $Python) {
    Write-Host "未找到可用的真实 python.exe（已跳过 PyManager 启动器）。请安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red
    exit 1
}
Write-Host "使用 Python: $Python"

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

    # 构造动作：python checkin.py（可带额外参数，如 --qoder-wait 15）
    # 日志由 checkin.py 内部的 logging.FileHandler 写入 logs\checkin.log，不需要 shell 重定向
    $argLine = "`"$CheckinScript`""
    if ($ScriptArgs) { $argLine += " $ScriptArgs" }
    $action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument $argLine `
        -WorkingDirectory $ScriptDir

    # 每天定时触发
    $trigger = New-ScheduledTaskTrigger -Daily -At $t

    # 允许在电池供电时也运行，失败后重试
    # 时限 20 分钟：Trae UI 兜底冷启动 + 自动更新安装最坏情况约 11 分钟
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
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
