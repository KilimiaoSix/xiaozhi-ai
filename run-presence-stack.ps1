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
    [string]$ServerPython = "",
    [switch]$Preview
)

$ErrorActionPreference = "Stop"
$agentRoot = Join-Path $PSScriptRoot "presence-agent"
$agentPython = Join-Path $agentRoot ".venv\Scripts\python.exe"
$serverRoot = Join-Path $PSScriptRoot "server\main\xiaozhi-server"
$runtimeRoot = Join-Path $agentRoot ".runtime"
$startedServer = $null
$previousToken = $env:PRESENCE_AUTH_TOKEN

function Test-PresenceServer {
    param([string]$BaseUrl)
    $headers = @{}
    if ($AuthToken) { $headers["Authorization"] = "Bearer $AuthToken" }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Headers $headers -Uri ($BaseUrl.TrimEnd('/') + "/xiaozhi/presence/__healthcheck__")
        return $response.StatusCode -eq 200
    }
    catch {
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            return $status -eq 401 -or $status -eq 404
        }
        return $false
    }
}

function Wait-PresenceServer {
    param([string]$BaseUrl, [int]$TimeoutSeconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-PresenceServer -BaseUrl $BaseUrl) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

try {
    if ($AuthToken) { $env:PRESENCE_AUTH_TOKEN = $AuthToken }

    & (Join-Path $agentRoot "setup.ps1") -PythonExe $PythonExe
    if (-not $?) { throw "presence-agent setup failed" }

    if (-not (Test-PresenceServer -BaseUrl $ServerUrl)) {
        $uri = [Uri]$ServerUrl
        if ($uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
            throw "Remote presence Server is unavailable. Start it first or check -ServerUrl."
        }

        New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
        $stdout = Join-Path $runtimeRoot "presence-server.out.log"
        $stderr = Join-Path $runtimeRoot "presence-server.err.log"

        if ($ServerPython) {
            $startedServer = Start-Process -FilePath $ServerPython -ArgumentList @("app.py") -WorkingDirectory $serverRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        }
        else {
            $presenceServer = Join-Path $serverRoot "presence_server.py"
            $startedServer = Start-Process -FilePath $agentPython -ArgumentList @($presenceServer, "--host", $uri.Host, "--port", $uri.Port) -WorkingDirectory $serverRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        }

        if (-not (Wait-PresenceServer -BaseUrl $ServerUrl)) {
            $details = if (Test-Path -LiteralPath $stderr) { (Get-Content -Raw -LiteralPath $stderr) } else { "No server error log was produced." }
            throw "Presence Server did not become ready.`n$details"
        }
    }

    $runParameters = @{
        ServerUrl = $ServerUrl
        WorkstationId = $WorkstationId
        Camera = $Camera
        Width = $Width
        Height = $Height
        AbsentAfter = $AbsentAfter
        HeartbeatSeconds = $HeartbeatSeconds
        CameraRetrySeconds = $CameraRetrySeconds
        SmokeFrames = $SmokeFrames
        PythonExe = $PythonExe
        Preview = $Preview
    }
    if ($Model) { $runParameters["Model"] = $Model }

    & (Join-Path $agentRoot "run.ps1") @runParameters
    exit $LASTEXITCODE
}
finally {
    if ($startedServer -and -not $startedServer.HasExited) {
        Stop-Process -Id $startedServer.Id -Force
    }
    $env:PRESENCE_AUTH_TOKEN = $previousToken
}
