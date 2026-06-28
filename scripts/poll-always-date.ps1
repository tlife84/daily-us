$ErrorActionPreference = 'Continue'
Set-Location 'C:\Workspace\daily-us'
$logPath = 'C:\Workspace\daily-us\logs\always-date.log'
$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$startedAt] START watcher=always_date" | Add-Content -Path $logPath -Encoding UTF8
& 'C:\Workspace\daily-us\.venv\Scripts\python.exe' -m daily_us poll --watcher always_date *>> $logPath
$exitCode = if ($LASTEXITCODE -eq $null) { 0 } else { $LASTEXITCODE }
$endedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$endedAt] END watcher=always_date exit=$exitCode" | Add-Content -Path $logPath -Encoding UTF8
exit $exitCode
