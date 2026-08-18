param(
    [string]$PythonExe = "python",
    [switch]$IncludeTest
)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$requirements = if ($IncludeTest) { "requirements-test.txt" } else { "requirements.txt" }
$requirementsPath = Join-Path $PSScriptRoot $requirements
$markerPath = Join-Path $PSScriptRoot ".venv\requirements.sha256"

Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        & $PythonExe -m venv .venv
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsPath).Hash
    $installedHash = if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        (Get-Content -Raw -LiteralPath $markerPath).Trim()
    } else {
        ""
    }

    if ($installedHash -ne $requirementsHash) {
        & $venvPython -m pip install --disable-pip-version-check -r $requirements
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Set-Content -LiteralPath $markerPath -Value $requirementsHash -Encoding ASCII
    }
}
finally {
    Pop-Location
}
