[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action,
    [switch]$IncludeOCR
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunRoot = Join-Path $ProjectRoot '.data\run'
$LogRoot = Join-Path $ProjectRoot '.data\logs'
$PaddleXCacheRoot = Join-Path $ProjectRoot '.data\paddlex-cache'

if ($IncludeOCR) {
    $env:PADDLE_PDX_CACHE_HOME = $PaddleXCacheRoot
}

function Get-ServiceDefinitions {
    $services = @(
        @{
            Name = 'backend'
            File = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
            Arguments = @('-m', 'uvicorn', 'invoice_intelligence.main:app', '--host', '127.0.0.1', '--port', '8000', '--loop', 'asyncio:SelectorEventLoop')
            WorkingDirectory = $ProjectRoot
            Probe = 'http://127.0.0.1:8000/api/v1/health'
        },
        @{
            Name = 'memory-worker'
            File = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
            Arguments = @('-m', 'invoice_intelligence.workers.memory_admission')
            WorkingDirectory = $ProjectRoot
            Probe = $null
        },
        @{
            Name = 'frontend'
            File = (Get-Command npm.cmd -ErrorAction Stop).Source
            Arguments = @('--prefix', (Join-Path $ProjectRoot 'frontend'), 'run', 'dev', '--', '--host', '127.0.0.1')
            WorkingDirectory = $ProjectRoot
            Probe = 'http://127.0.0.1:5173'
        }
    )
    if ($IncludeOCR) {
        $services += @{
            Name = 'ocr'
            File = Join-Path $ProjectRoot '.venv-ocr\Scripts\paddlex.exe'
            Arguments = @('--serve', '--pipeline', (Join-Path $ProjectRoot 'conf\ocr\ppocrv6_small_v1.yaml'), '--host', '127.0.0.1', '--port', '8188', '--device', 'gpu:0')
            WorkingDirectory = $ProjectRoot
            Probe = 'tcp:127.0.0.1:8188'
        }
    }
    return $services
}

function Get-ManagedProcess([hashtable]$Service) {
    $pidFile = Join-Path $RunRoot "$($Service.Name).pid"
    if (!(Test-Path -LiteralPath $pidFile)) { return $null }
    $managedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $managedPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $pidFile -Force
        return $null
    }
    $command = (Get-CimInstance Win32_Process -Filter "ProcessId=$managedPid").CommandLine
    if ([string]::IsNullOrWhiteSpace($command) -or !$command.Contains($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "PID file $pidFile does not identify a process owned by $ProjectRoot"
    }
    return $process
}

function Get-ProbeStatus([string]$Probe) {
    if ([string]::IsNullOrWhiteSpace($Probe)) { return 'not-applicable' }
    if ($Probe.StartsWith('tcp:')) {
        $parts = $Probe.Substring(4).Split(':')
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $connected = $client.ConnectAsync($parts[0], [int]$parts[1]).Wait(1000)
            return $(if ($connected -and $client.Connected) { 'ready' } else { 'unreachable' })
        } catch { return 'unreachable' } finally { $client.Dispose() }
    }
    try {
        Invoke-WebRequest -Uri $Probe -Method Get -TimeoutSec 2 -UseBasicParsing | Out-Null
        return 'ready'
    } catch { return 'unreachable' }
}

New-Item -ItemType Directory -Force -Path $RunRoot, $LogRoot | Out-Null
$services = Get-ServiceDefinitions

if ($Action -eq 'start') {
    $alembic = Join-Path $ProjectRoot '.venv\Scripts\alembic.exe'
    if (!(Test-Path -LiteralPath $alembic)) {
        throw "Alembic executable not found: $alembic. Run pip install -e . first."
    }
    Write-Host 'Applying PostgreSQL migrations...' -ForegroundColor Cyan
    & $alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed with exit code $LASTEXITCODE"
    }
}

foreach ($service in $services) {
    $process = Get-ManagedProcess $service
    if ($Action -eq 'start') {
        if ($null -ne $process) {
            [pscustomobject]@{ Service = $service.Name; Process = 'running'; Probe = Get-ProbeStatus $service.Probe; Pid = $process.Id }
            continue
        }
        if (!(Test-Path -LiteralPath $service.File)) { throw "Executable not found: $($service.File)" }
        $stdout = Join-Path $LogRoot "$($service.Name).stdout.log"
        $stderr = Join-Path $LogRoot "$($service.Name).stderr.log"
        $process = Start-Process -FilePath $service.File -ArgumentList $service.Arguments -WorkingDirectory $service.WorkingDirectory -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath (Join-Path $RunRoot "$($service.Name).pid") -Value $process.Id -Encoding ascii
        [pscustomobject]@{ Service = $service.Name; Process = 'started'; Probe = 'starting'; Pid = $process.Id }
        continue
    }
    if ($Action -eq 'stop') {
        if ($null -eq $process) {
            [pscustomobject]@{ Service = $service.Name; Process = 'stopped'; Probe = 'not-applicable'; Pid = $null }
            continue
        }
        Stop-Process -Id $process.Id
        Remove-Item -LiteralPath (Join-Path $RunRoot "$($service.Name).pid") -Force
        [pscustomobject]@{ Service = $service.Name; Process = 'stopped'; Probe = 'not-applicable'; Pid = $process.Id }
        continue
    }
    [pscustomobject]@{
        Service = $service.Name
        Process = $(if ($null -eq $process) { 'stopped' } else { 'running' })
        Probe = $(if ($null -eq $process) { 'unreachable' } else { Get-ProbeStatus $service.Probe })
        Pid = $(if ($null -eq $process) { $null } else { $process.Id })
    }
}
