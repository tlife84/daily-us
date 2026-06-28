$ErrorActionPreference = 'Continue'
Set-Location 'C:\Workspace\daily-us'
$logPath = 'C:\Workspace\daily-us\logs\good-morning.log'
$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$startedAt] START watcher=good_morning_damsaem" | Add-Content -Path $logPath -Encoding UTF8
& 'C:\Workspace\daily-us\.venv\Scripts\python.exe' -m daily_us poll --watcher good_morning_damsaem *>> $logPath
$exitCode = if ($LASTEXITCODE -eq $null) { 0 } else { $LASTEXITCODE }
$endedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$endedAt] END watcher=good_morning_damsaem exit=$exitCode" | Add-Content -Path $logPath -Encoding UTF8
exit $exitCode
