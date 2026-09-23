$ErrorActionPreference = 'Continue'
Set-Location 'C:\Workspace\daily-us'
$logPath = 'C:\Workspace\daily-us\logs\regular-class.log'
# 최초 실행에도 로그 경로 생성. 절전 복귀로 늦게 시작하면 Python의 한국 시간 스케줄 재확인
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null
$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$startedAt] START watcher=regular_class" | Add-Content -Path $logPath -Encoding UTF8
& 'C:\Workspace\daily-us\.venv\Scripts\python.exe' -m daily_us poll --watcher regular_class --respect-schedule *>> $logPath
$exitCode = if ($LASTEXITCODE -eq $null) { 0 } else { $LASTEXITCODE }
$endedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$endedAt] END watcher=regular_class exit=$exitCode" | Add-Content -Path $logPath -Encoding UTF8
exit $exitCode
