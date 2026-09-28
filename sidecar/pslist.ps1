param([switch]$Kill)
$pat = 'sgcc_sidecar|chrome-profile|cp-dbg|cp-int|fp-'
$rows = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -match '^(python|python3|chromedriver|chrome|google-chrome)\.exe$' -and $_.CommandLine -match $pat
}
if (-not $rows) { Write-Output "no matching processes"; exit 0 }
foreach ($p in $rows) {
  $cl = $p.CommandLine
  if ($cl.Length -gt 150) { $cl = $cl.Substring(0,150) }
  Write-Output ("{0,-16} PID={1,-7} {2}" -f $p.Name, $p.ProcessId, $cl)
  if ($Kill) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}
if ($Kill) { Write-Output "killed $($rows.Count) process(es)" }
