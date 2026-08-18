param(
    [string]$ServerUrl = "http://127.0.0.1:8003",
    [string]$WorkstationId = $env:COMPUTERNAME,
    [string]$AuthToken = $env:PRESENCE_AUTH_TOKEN,
    [int]$Camera = 0,
    [int]$Width = 640,
    [int]$Height = 480,
    [double]$AbsentAfter = 2.0,
    [double]$HeartbeatSeconds = 15.0,
    [double]$CameraRetrySeconds = 5.0,
    [string]$Model = "",
    [int]$SmokeFrames = 0,
    [string]$PythonExe = "python",
    [switch]$Preview
)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$previousToken = $env:PRESENCE_AUTH_TOKEN

& (Join-Path $PSScriptRoot "setup.ps1") -PythonExe $PythonExe
if (-not $?) { throw "presence-agent setup failed" }

$normalizedWorkstationId = ($WorkstationId -replace '[^A-Za-z0-9._-]', '-').Trim('-', '.')
if (-not $normalizedWorkstationId) {
    throw "WorkstationId must contain at least one supported character."
}
if ($normalizedWorkstationId.Length -gt 64) {
    $normalizedWorkstationId = $normalizedWorkstationId.Substring(0, 64)
}

$arguments = @(
    "-m", "presence_agent.app",
    "--server-url", $ServerUrl,
    "--workstation-id", $normalizedWorkstationId,
    "--camera", $Camera,
    "--width", $Width,
    "--height", $Height,
    "--absent-after", $AbsentAfter,
    "--heartbeat-seconds", $HeartbeatSeconds,
    "--camera-retry-seconds", $CameraRetrySeconds,
    "--smoke-frames", $SmokeFrames
)
if ($Model) {
    $arguments += @("--model", $Model)
}
if ($Preview) {
    $arguments += "--preview"
}
Push-Location $PSScriptRoot
try {
    if ($AuthToken) { $env:PRESENCE_AUTH_TOKEN = $AuthToken }
    & $venvPython @arguments
    exit $LASTEXITCODE
}
finally {
    $env:PRESENCE_AUTH_TOKEN = $previousToken
    Pop-Location
}
