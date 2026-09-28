[CmdletBinding()]
param(
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start',
    [ValidateSet('Auto', 'Docker', 'Host')]
    [string]$OcrMode = 'Auto',
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = @(
    'compose', '-p', 'invoice-acceptance',
    '--env-file', (Join-Path $ProjectRoot '.env.compose'),
    '-f', (Join-Path $ProjectRoot 'docker-compose.yml'),
    '-f', (Join-Path $ProjectRoot 'compose.acceptance.yml')
)
$dockerOcr = Join-Path $ProjectRoot 'compose.ocr-gpu.yml'
$services = @(
    'postgres', 'postgres-checkpoint', 'postgres-auth', 'keycloak',
    'etcd', 'object-storage', 'milvus', 'api', 'extraction-worker',
    'memory-admission', 'index-rebuild'
)

function Test-HostOcr {
    try {
        $response = Invoke-RestMethod -Uri 'http://127.0.0.1:8077/openapi.json' -TimeoutSec 3
        return $null -ne $response.paths.'/ocr'
    } catch {
        return $false
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

$hostOcrReady = Test-HostOcr
$dockerOcrIds = @(& docker ps --filter 'label=com.docker.compose.project=invoice-acceptance' --filter 'label=com.docker.compose.service=ocr' --format '{{.ID}}')
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the Docker OCR service.' }
$dockerOcrRunning = $dockerOcrIds.Count -gt 0
if ($Action -eq 'start' -and $hostOcrReady -and $dockerOcrRunning) {
    throw 'Both host and Docker OCR are running. Stop one before starting the acceptance stack.'
}
$useDockerOcr = $Action -ne 'start' -or $OcrMode -eq 'Docker' -or ($OcrMode -eq 'Auto' -and ($dockerOcrRunning -or -not $hostOcrReady))
if ($Action -eq 'start' -and $OcrMode -eq 'Host' -and -not $hostOcrReady) {
    throw 'Host OCR at 127.0.0.1:8077 is unavailable.'
}
if ($Action -eq 'start' -and $OcrMode -eq 'Docker' -and $hostOcrReady) {
    throw 'Host OCR already occupies the GPU. Stop it before starting Docker OCR, or use -OcrMode Auto.'
}
if ($useDockerOcr) {
    $compose += @('-f', $dockerOcr)
}
$compose += @('--profile', 'core', '--profile', 'auth', '--profile', 'vector')

Push-Location $ProjectRoot
try {
    switch ($Action) {
        'start' {
            if ($useDockerOcr -and -not $Build) {
                $ocrImageIds = @(& docker image ls --format '{{.ID}}' --filter 'reference=invoice-intelligence-ocr:ppocrv6-small-v1')
                if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the Docker OCR image.' }
                if ($ocrImageIds.Count -eq 0) { Invoke-Compose @('build', 'ocr') }
            }
            $arguments = @('up', '-d')
            if ($Build) { $arguments += '--build' }
            $arguments += $services
            if ($useDockerOcr) { $arguments += 'ocr' }
            Invoke-Compose $arguments
            Write-Host "OCR mode: $(if ($useDockerOcr) { 'Docker gpu:0, internal port 8077' } else { 'host 127.0.0.1:8077' })"
            Invoke-Compose @('ps', 'api', 'extraction-worker', 'memory-admission')
        }
        'status' {
            Invoke-Compose @('ps')
            Write-Host "Host OCR 8077: $(if ($hostOcrReady) { 'ready' } else { 'unavailable' })"
        }
        'stop' {
            Invoke-Compose @('stop')
        }
    }
} finally {
    Pop-Location
}
