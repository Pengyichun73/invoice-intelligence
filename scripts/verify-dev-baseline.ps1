[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found: $python"
}

Push-Location $projectRoot
$probePath = Join-Path $projectRoot ".data\dev-baseline-probe.py"
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $probePath) | Out-Null
    $code = @'
from invoice_intelligence.config import get_settings
from invoice_intelligence.infrastructure.preflight import run_preflight

settings = get_settings()
if settings.environment.value != "development":
    raise SystemExit("Development baseline requires INVOICE_INTELLIGENCE_ENVIRONMENT=development")
if settings.auth_mode != "development":
    raise SystemExit("Development baseline requires development auth mode")
if not settings.dev_tenant_id or not settings.dev_tenant_id.strip():
    raise SystemExit("Development baseline requires INVOICE_INTELLIGENCE_DEV_TENANT_ID")
result = run_preflight(settings)
if not result.ready:
    raise SystemExit(f"Development preflight failed: {result.checks}")

import invoice_intelligence.main  # noqa: F401

print("development-baseline-ok")
'@
    Set-Content -LiteralPath $probePath -Value $code -Encoding utf8
    & $python $probePath
    if ($LASTEXITCODE -ne 0) {
        throw "Development baseline import/configuration check failed"
    }

    & $python -m compileall -q src
    if ($LASTEXITCODE -ne 0) {
        throw "Python compilation failed"
    }
}
finally {
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
    Pop-Location
}
