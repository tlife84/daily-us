$ErrorActionPreference = 'Continue'
Set-Location 'C:\Workspace\daily-us'
$logPath = 'C:\Workspace\daily-us\logs\company-analysis-guide.log'
$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$startedAt] START watcher=company_analysis_guide" | Add-Content -Path $logPath -Encoding UTF8
& 'C:\Workspace\daily-us\.venv\Scripts\python.exe' -m daily_us poll --watcher company_analysis_guide *>> $logPath
$exitCode = if ($LASTEXITCODE -eq $null) { 0 } else { $LASTEXITCODE }
$endedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'
"[$endedAt] END watcher=company_analysis_guide exit=$exitCode" | Add-Content -Path $logPath -Encoding UTF8
exit $exitCode
