$ErrorActionPreference = 'Stop'
$root = 'C:\Workspace\daily-us'

# A안: 반복(Repetition) 트리거 대신 고정 시각의 개별 트리거를 만든다.
# 반복 트리거는 Modern Standby에서 늦게 복귀하면 "늦은 실행 시각" 기준으로
# 반복 간격이 재계산(re-anchor)되어 그날 격자 전체가 밀린다. 개별 트리거는
# 하나가 StartWhenAvailable로 늦게 실행돼도 나머지 시각을 흔들지 않는다.
# 시각은 config.yaml 의 active_hours / interval_minutes 와 일치시킨다.

function New-FixedTriggers {
    param(
        [Parameter(Mandatory)] [string]$Start,
        [Parameter(Mandatory)] [string]$End,
        [Parameter(Mandatory)] [int]$IntervalMinutes,
        [string]$DayOfWeek  # 지정 시 주간 트리거, 미지정 시 매일 트리거
    )
    $t = [datetime]::ParseExact($Start, 'HH:mm', $null)
    $endT = [datetime]::ParseExact($End, 'HH:mm', $null)
    $triggers = @()
    while ($t -le $endT) {
        if ($DayOfWeek) {
            $triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $t
        }
        else {
            $triggers += New-ScheduledTaskTrigger -Daily -At $t
        }
        $t = $t.AddMinutes($IntervalMinutes)
    }
    return $triggers
}

$jobs = @(
    @{
        TaskName = 'daily-us good morning'
        Script   = 'poll-good-morning.ps1'
        Limit    = 'PT9M'
        Triggers = (New-FixedTriggers -Start '07:00' -End '09:10' -IntervalMinutes 10)
    },
    @{
        TaskName = 'daily-us always date'
        Script   = 'poll-always-date.ps1'
        Limit    = 'PT50M'
        Triggers = (New-FixedTriggers -Start '07:00' -End '22:00' -IntervalMinutes 60)
    },
    @{
        TaskName = 'daily-us company analysis guide'
        Script   = 'poll-company-analysis-guide.ps1'
        Limit    = 'PT50M'
        Triggers = (New-FixedTriggers -Start '12:00' -End '20:00' -IntervalMinutes 60 -DayOfWeek 'Sunday')
    }
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
    Set-ScheduledTask -TaskName $job.TaskName -Action $action -Trigger $job.Triggers -Settings $task.Settings -Principal $principal | Out-Null
    Write-Host ("Updated {0}: {1} fixed trigger(s)" -f $job.TaskName, $job.Triggers.Count)
}

wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setactive SCHEME_CURRENT

Write-Host 'daily-us scheduled tasks updated.'
