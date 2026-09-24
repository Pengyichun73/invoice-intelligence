[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}

$CoreFiles = @(
    'src/invoice_intelligence/bootstrap.py',
    'src/invoice_intelligence/config/settings.py',
    'src/invoice_intelligence/domain/invoice.py',
    'src/invoice_intelligence/domain/field_semantics.py',
    'src/invoice_intelligence/domain/governance.py',
    'src/invoice_intelligence/application/ports/observability.py',
    'src/invoice_intelligence/application/services/extraction_validation.py',
    'src/invoice_intelligence/application/services/field_semantic_binding.py',
    'src/invoice_intelligence/application/services/field_semantic_catalog.py',
    'src/invoice_intelligence/application/services/field_semantic_index_projection.py',
    'src/invoice_intelligence/application/services/memory_governance.py',
    'src/invoice_intelligence/application/services/multi_source_ocr_comparison.py',
    'src/invoice_intelligence/application/services/vision_extraction.py',
    'src/invoice_intelligence/infrastructure/corrections/scopes.py',
    'src/invoice_intelligence/infrastructure/observability/ocr.py',
    'src/invoice_intelligence/infrastructure/ocr/paddlex_http.py',
    'src/invoice_intelligence/infrastructure/persistence/sqlalchemy_models.py',
    'src/invoice_intelligence/infrastructure/persistence/sqlalchemy_admission.py',
    'src/invoice_intelligence/infrastructure/qwen/vision.py',
    'src/invoice_intelligence/infrastructure/vision/structured.py',
    'src/invoice_intelligence/api/presenters.py',
    'src/invoice_intelligence/api/routes/field_semantics.py',
    'src/invoice_intelligence/api/routes/memory.py',
    'src/invoice_intelligence/api/schemas/common.py',
    'src/invoice_intelligence/api/schemas/memory.py',
    'src/invoice_intelligence/workers/memory_admission.py',
    'tests'
)

Push-Location $ProjectRoot
try {
    & $Python -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }

    & $Python -m mypy
    if ($LASTEXITCODE -ne 0) { throw 'mypy failed' }

    & $Python -m ruff check --select E,F,I @CoreFiles
    if ($LASTEXITCODE -ne 0) { throw 'ruff failed' }

    Push-Location (Join-Path $ProjectRoot 'frontend')
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}
