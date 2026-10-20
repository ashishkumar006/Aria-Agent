# start_aria.ps1 - bring up the gateway + agent, then open the console.
#
#   powershell -ExecutionPolicy Bypass -File start_aria.ps1
#   powershell -ExecutionPolicy Bypass -File start_aria.ps1 -NoBrowser
#
# Safe to re-run: anything already listening on the ports is reused, not
# restarted. Logs land in .\logs\.

param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$agentDir = Join-Path $root 'S9SharedCode\code'
$gwDir = Join-Path $root 'llm_gatewayV9'
$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Test-Listening([int]$port) {
  try {
    $c = New-Object System.Net.Sockets.TcpClient
    $c.Connect('127.0.0.1', $port)
    $c.Close()
    return $true
  } catch { return $false }
}

function Start-Svc([string]$name, [string]$dir, [string]$script, [int]$port) {
  if (Test-Listening $port) {
    Write-Host "[aria] $name already listening on :$port"
    return
  }
  Write-Host "[aria] starting $name on :$port ..."
  $out = Join-Path $logDir "$name.out.log"
  $err = Join-Path $logDir "$name.err.log"
  Start-Process -WindowStyle Hidden -FilePath 'cmd.exe' -ArgumentList @(
    '/c', "cd /d `"$dir`" && uv run $script > `"$out`" 2> `"$err`""
  ) | Out-Null
}

Start-Svc 'gateway' $gwDir 'main.py' 8109
Start-Svc 'agent'   $agentDir 'agent_server.py' 8500

$deadline = (Get-Date).AddSeconds(90)
$health = $null
while ((Get-Date) -lt $deadline) {
  try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8500/api/health' -TimeoutSec 3
    if ($health.agent -eq 'ready') { break }
  } catch { }
  Start-Sleep -Milliseconds 750
}

if (-not $health -or $health.agent -ne 'ready') {
  Write-Host "[aria] agent did not become ready in 90s - see $logDir\agent.err.log" -ForegroundColor Yellow
  exit 1
}

# The gateway boots slower than the agent (imports + provider init). The
# agent's health re-probes it per call, so give it a grace window before
# calling it a failure instead of warning on the first false reading.
$gwDeadline = (Get-Date).AddSeconds(60)
while ($health.gateway_up -ne $true -and (Get-Date) -lt $gwDeadline) {
  Start-Sleep -Milliseconds 1000
  try { $health = Invoke-RestMethod 'http://127.0.0.1:8500/api/health' -TimeoutSec 3 } catch { }
}

Write-Host ("[aria] ready  gateway_up={0}  spa_built={1}" -f $health.gateway_up, $health.spa_built)
if ($health.gateway_up -ne $true) {
  Write-Host "[aria] warning: gateway not up after 60s - see $logDir\gateway.err.log; model calls will fail until it is" -ForegroundColor Yellow
}

if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8500/' }
