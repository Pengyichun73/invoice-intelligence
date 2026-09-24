[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$documents = @(
    (Join-Path $projectRoot "README.md"),
    (Join-Path $projectRoot "INVOICE_INTELLIGENCE_SOLUTION.md")
)
$documents += Get-ChildItem -LiteralPath (Join-Path $projectRoot "docs") -Filter "*.md" -File |
    Select-Object -ExpandProperty FullName

$errors = [System.Collections.Generic.List[string]]::new()
$linkPattern = '\[[^\]]+\]\(([^)]+)\)'

foreach ($document in $documents) {
    $content = Get-Content -LiteralPath $document -Raw -Encoding UTF8
    $relativeDocument = $document.Substring($projectRoot.Length + 1)

    foreach ($match in [regex]::Matches($content, $linkPattern)) {
        $target = $match.Groups[1].Value.Trim()
        if ([string]::IsNullOrWhiteSpace($target) -or
            $target.StartsWith("#") -or
            $target -match '^(https?|mailto):') {
            continue
        }

        $targetPath = ($target -split "#", 2)[0].Trim("<>")
        if ([string]::IsNullOrWhiteSpace($targetPath)) {
            continue
        }

        $resolvedTarget = Join-Path (Split-Path -Parent $document) $targetPath
        if (-not (Test-Path -LiteralPath $resolvedTarget -PathType Leaf)) {
            $errors.Add("$relativeDocument -> $target")
        }
    }
}

$statusPath = Join-Path $projectRoot "docs/project-status.md"
$statusContent = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8
foreach ($requiredText in @(
    "本文是 Invoice Intelligence 当前实现进度的唯一状态基线",
    "状态核对日期：",
    "## 当前不得对外宣称",
    "## 后续交付记录"
)) {
    if ($statusContent -notlike "*$requiredText*") {
        $errors.Add("docs/project-status.md 缺少状态基线标记：$requiredText")
    }
}

$stalePatterns = @(
    'profile [`"]indexing[`"]',
    '本阶段未执行任何评估。',
    '单文件前端',
    '不存在交易分类'
)
foreach ($document in $documents) {
    $content = Get-Content -LiteralPath $document -Raw -Encoding UTF8
    foreach ($pattern in $stalePatterns) {
        if ($content -match $pattern) {
            $errors.Add("$($document.Substring($projectRoot.Length + 1)) 包含过时状态声明：$pattern")
        }
    }
}

if ($errors.Count -gt 0) {
    Write-Error ("文档持续检查失败：`n- " + ($errors -join "`n- "))
    exit 1
}

Write-Output "文档持续检查通过：$($documents.Count) 个核心文档，链接与状态声明均无已知问题。"
