$targets = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -match 'python|chromedriver|chrome' -and $_.CommandLine -match 'sgcc_sidecar|watch-only|chrome-profile'
}
foreach ($p in $targets) {
  Write-Output ("stopping PID {0} {1}" -f $p.ProcessId, $p.Name)
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not $targets) { Write-Output "nothing matched" }
