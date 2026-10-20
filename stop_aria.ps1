# stop_aria.ps1 - stop whatever is listening on the agent/gateway ports.
#
#   powershell -ExecutionPolicy Bypass -File stop_aria.ps1
#
# Find the listener per port, stop it, report what was stopped.

param([int[]]$Ports = @(8500, 8109))

foreach ($p in $Ports) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if (-not $conns) {
    Write-Host "[aria] nothing listening on :$p"
    continue
  }
  $owners = $conns | Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($procId in $owners) {
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) {
      Write-Host "[aria] pid $procId on :$p already gone"
      continue
    }
    try {
      Stop-Process -Id $procId -Force
      Write-Host "[aria] stopped $($proc.ProcessName) (pid $procId) on :$p"
    } catch {
      Write-Host "[aria] could not stop $($proc.ProcessName) (pid $procId) on :$p - $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }
}
