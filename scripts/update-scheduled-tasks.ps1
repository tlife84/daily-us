$ErrorActionPreference = 'Stop'
$root = 'C:\Workspace\daily-us'
$jobs = @(
    @{ TaskName = 'daily-us good morning'; Script = 'poll-good-morning.ps1'; Limit = 'PT9M' },
    @{ TaskName = 'daily-us always date'; Script = 'poll-always-date.ps1'; Limit = 'PT50M' },
    @{ TaskName = 'daily-us company analysis guide'; Script = 'poll-company-analysis-guide.ps1'; Limit = 'PT50M' }
)

foreach ($job in $jobs) {
    $scriptPath = Join-Path (Join-Path $root 'scripts') $job.Script
    $task = Get-ScheduledTask -TaskName $job.TaskName
    $task.Settings.WakeToRun = $true
    $task.Settings.StartWhenAvailable = $true
    $task.Settings.ExecutionTimeLimit = $job.Limit
    # (2) 복귀 직후 네트워크 미복구 시 실패 대신 대기/재시도
    $task.Settings.RunOnlyIfNetworkAvailable = $true
    $action = New-ScheduledTaskAction -Execute 'C:\Program Files\PowerShell\7\pwsh.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" -WorkingDirectory $root
    # (1) 대화형 세션 대신 S4U(로그온 여부 무관, 세션 0)로 실행 → Modern Standby 세션 끊김 영향 없음
    $principal = New-ScheduledTaskPrincipal -UserId $task.Principal.UserId -LogonType S4U -RunLevel Highest
    Set-ScheduledTask -TaskName $job.TaskName -Action $action -Settings $task.Settings -Principal $principal | Out-Null
}

wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setactive SCHEME_CURRENT

Write-Host 'daily-us scheduled tasks updated.'
