[CmdletBinding()]
param(
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start',
    [switch]$Build,
    [switch]$IncludeFrontend
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunRoot = Join-Path $ProjectRoot '.data\run'
$LogRoot = Join-Path $ProjectRoot '.data\logs'
$OcrExecutable = Join-Path $ProjectRoot '.venv-ocr\Scripts\paddlex.exe'
$OcrPipeline = Join-Path $ProjectRoot 'conf\ocr\ppocrv6_small_v1.yaml'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$ViteEntry = Join-Path $FrontendRoot 'node_modules\vite\bin\vite.js'
$compose = @(
    'compose', '-p', 'invoice-intelligence',
    '--env-file', (Join-Path $ProjectRoot '.env.compose'),
    '-f', (Join-Path $ProjectRoot 'docker-compose.yml'),
    '-f', (Join-Path $ProjectRoot 'compose.local-oidc.yml')
)
$services = @(
    'postgres', 'postgres-checkpoint', 'postgres-auth', 'keycloak',
    'etcd', 'object-storage', 'milvus', 'api', 'extraction-worker',
    'invoice-segmentation-worker',
    'memory-admission', 'index-rebuild', 'conflict-reevaluation',
    'transaction-analysis-worker', 'storage-lifecycle-worker'
)

function Test-HostOcr {
    try {
        $response = Invoke-RestMethod -Uri 'http://127.0.0.1:8077/openapi.json' -TimeoutSec 3
        return $null -ne $response.paths.'/ocr'
    } catch {
        return $false
    }
}

function Test-Frontend {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:15173/' -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200 -and $response.Content.Contains('/src/main.js')
    } catch {
        return $false
    }
}

function Test-PortOpen([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connected = $client.ConnectAsync('127.0.0.1', $Port).Wait(1000)
        return $connected -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-ManagedProcess([string]$Name, [string]$Marker) {
    $pidFile = Join-Path $RunRoot "$Name.pid.json"
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return $null }
    $record = Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
    try { $managedPid = [int]$record.pid } catch { throw "Invalid managed PID: $pidFile" }
    if ($managedPid -le 0 -or -not $record.started_at_ticks) {
        throw "Invalid managed process record: $pidFile"
    }
    $process = Get-Process -Id $managedPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $pidFile -Force
        return $null
    }
    $command = (Get-CimInstance Win32_Process -Filter "ProcessId=$managedPid").CommandLine
    $started = [long]$record.started_at_ticks
    if (
        [string]::IsNullOrWhiteSpace($command) -or
        $command.IndexOf($Marker, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        [math]::Abs($process.StartTime.ToUniversalTime().Ticks - $started) -gt 20000000
    ) {
        throw "PID record does not identify this project's $Name process: $pidFile"
    }
    return $process
}

function Save-ManagedProcess([string]$Name, [Diagnostics.Process]$Process) {
    New-Item -ItemType Directory -Force -Path $RunRoot | Out-Null
    $record = @{
        pid = $Process.Id
        started_at_ticks = $Process.StartTime.ToUniversalTime().Ticks
    } | ConvertTo-Json
    [IO.File]::WriteAllText(
        (Join-Path $RunRoot "$Name.pid.json"),
        $record,
        [Text.UTF8Encoding]::new($false)
    )
}

function Stop-ManagedService([string]$Name, [string]$Marker) {
    $process = Get-ManagedProcess $Name $Marker
    if ($null -eq $process) { return }
    Stop-Process -Id $process.Id
    Remove-Item -LiteralPath (Join-Path $RunRoot "$Name.pid.json") -Force
}

function Wait-ForService([scriptblock]$Probe, [string]$Name, [string]$Marker, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Probe) { return }
        if ($null -eq (Get-ManagedProcess $Name $Marker)) {
            throw "$Name process exited before it became ready; inspect .data/logs/$Name.stderr.log"
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name did not become ready within $Seconds seconds; inspect .data/logs/$Name.stderr.log"
}

function Start-HostOcr {
    $managed = Get-ManagedProcess 'intelligence-ocr' $OcrExecutable
    if (Test-HostOcr) { return }
    if ($null -ne $managed) {
        Wait-ForService { Test-HostOcr } 'intelligence-ocr' $OcrExecutable 180
        return
    }
    if (Test-PortOpen 8077) { throw 'Port 8077 is occupied by a service without the /ocr contract.' }
    if (-not (Test-Path -LiteralPath $OcrExecutable -PathType Leaf)) {
        throw "Missing OCR executable: $OcrExecutable"
    }
    if (-not (Test-Path -LiteralPath $OcrPipeline -PathType Leaf)) {
        throw "Missing OCR pipeline: $OcrPipeline"
    }
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $previousCache = $env:PADDLE_PDX_CACHE_HOME
    try {
        $env:PADDLE_PDX_CACHE_HOME = Join-Path $ProjectRoot '.data\paddlex-cache'
        $process = Start-Process -FilePath $OcrExecutable -ArgumentList @(
            '--serve', '--pipeline', "`"$OcrPipeline`"", '--host', '127.0.0.1',
            '--port', '8077', '--device', 'gpu:0'
        ) -WorkingDirectory $ProjectRoot -RedirectStandardOutput (
            Join-Path $LogRoot 'intelligence-ocr.stdout.log'
        ) -RedirectStandardError (
            Join-Path $LogRoot 'intelligence-ocr.stderr.log'
        ) -WindowStyle Hidden -PassThru
    } finally {
        $env:PADDLE_PDX_CACHE_HOME = $previousCache
    }
    Save-ManagedProcess 'intelligence-ocr' $process
    try {
        Wait-ForService { Test-HostOcr } 'intelligence-ocr' $OcrExecutable 180
    } catch {
        Stop-ManagedService 'intelligence-ocr' $OcrExecutable
        throw
    }
}

function Start-Frontend {
    $managed = Get-ManagedProcess 'intelligence-frontend' $ViteEntry
    if (Test-Frontend) { return }
    if ($null -ne $managed) {
        Wait-ForService { Test-Frontend } 'intelligence-frontend' $ViteEntry 45
        return
    }
    if (Test-PortOpen 15173) { throw 'Port 15173 is occupied by another service.' }
    if (-not (Test-Path -LiteralPath $ViteEntry -PathType Leaf)) {
        throw 'Missing frontend dependencies. Run npm install in frontend first.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot '.env.acceptance') -PathType Leaf)) {
        throw 'Missing frontend/.env.acceptance.'
    }
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $process = Start-Process -FilePath $node -ArgumentList @(
        "`"$ViteEntry`"", '--mode', 'acceptance', '--host', '127.0.0.1',
        '--port', '15173', '--strictPort'
    ) -WorkingDirectory $FrontendRoot -RedirectStandardOutput (
        Join-Path $LogRoot 'intelligence-frontend.stdout.log'
    ) -RedirectStandardError (
        Join-Path $LogRoot 'intelligence-frontend.stderr.log'
    ) -WindowStyle Hidden -PassThru
    Save-ManagedProcess 'intelligence-frontend' $process
    try {
        Wait-ForService { Test-Frontend } 'intelligence-frontend' $ViteEntry 45
    } catch {
        Stop-ManagedService 'intelligence-frontend' $ViteEntry
        throw
    }
}

function Invoke-Compose([string[]]$Arguments) {
    & docker @compose @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose exited with code $LASTEXITCODE"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is not available.'
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env.compose'))) {
    throw 'Missing .env.compose.'
}

if ($Action -eq 'start') {
    $dockerOcrIds = @(& docker ps --filter 'label=com.docker.compose.project=invoice-intelligence' --filter 'label=com.docker.compose.service=ocr' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the Docker OCR service.' }
    if ($dockerOcrIds.Count -gt 0) {
        throw 'A Docker OCR container is still running. Stop it before using the host OCR service.'
    }
}
$compose += @('--profile', 'core', '--profile', 'auth', '--profile', 'vector', '--profile', 'object-storage')

Push-Location $ProjectRoot
try {
    switch ($Action) {
        'start' {
            Start-HostOcr
            $arguments = @('up', '-d')
            if ($Build) { $arguments += '--build' }
            $arguments += $services
            Invoke-Compose $arguments
            if ($IncludeFrontend) { Start-Frontend }
            Write-Host 'OCR mode: host 127.0.0.1:8077'
            Invoke-Compose @('ps', 'api', 'extraction-worker', 'memory-admission')
        }
        'status' {
            Invoke-Compose @('ps')
            Write-Host "Host OCR 8077: $(if (Test-HostOcr) { 'ready' } else { 'unavailable' })"
            Write-Host "Frontend 15173: $(if (Test-Frontend) { 'ready' } else { 'unavailable' })"
        }
        'stop' {
            Invoke-Compose @('stop')
            Stop-ManagedService 'intelligence-frontend' $ViteEntry
            Stop-ManagedService 'intelligence-ocr' $OcrExecutable
        }
    }
} finally {
    Pop-Location
}
