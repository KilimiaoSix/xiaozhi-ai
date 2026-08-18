param(
    [int]$Camera = 0,
    [int]$Width = 640,
    [int]$Height = 480,
    [int]$Samples = 20,
    [string]$DetectorModel = "",
    [string]$RecognizerModel = "",
    [string]$Template = "",
    [string]$PythonExe = "python",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

& (Join-Path $PSScriptRoot "setup.ps1") -PythonExe $PythonExe
if (-not $?) { throw "presence-agent setup failed" }

$arguments = @(
    "-m", "presence_agent.face_enrollment",
    "--camera", $Camera,
    "--width", $Width,
    "--height", $Height,
    "--samples", $Samples
)
if ($DetectorModel) { $arguments += @("--detector-model", $DetectorModel) }
if ($RecognizerModel) { $arguments += @("--recognizer-model", $RecognizerModel) }
if ($Template) { $arguments += @("--template", $Template) }
if ($Force) { $arguments += "--force" }

Push-Location $PSScriptRoot
try {
    & $venvPython @arguments
    $enrollmentExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($enrollmentExitCode -ne 0) {
    throw "owner face enrollment failed with exit code $enrollmentExitCode"
}
