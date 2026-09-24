[CmdletBinding()]
param(
    [ValidateSet("Plan", "Backup", "Restore")]
    [string]$Action = "Plan",
    [string]$ComposeProject = "invoice-intelligence-drill",
    [string]$BackupDirectory = ".\artifacts\backup-drill",
    [string]$RestoreProject = "",
    [switch]$Execute
)

$ErrorActionPreference = "Stop"

function Invoke-DrillStep {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )
    Write-Host "[drill] $Description"
    if ($Execute) {
        & $Command
        if ($LASTEXITCODE -ne 0) {
            throw "演练步骤失败：$Description"
        }
    } else {
        Write-Host "        dry-run（未执行）"
    }
}

if ($Action -ne "Plan" -and -not $Execute) {
    throw "Backup/Restore 必须显式提供 -Execute；默认只生成计划。"
}

$resolvedBackupDirectory = [System.IO.Path]::GetFullPath($BackupDirectory)
New-Item -ItemType Directory -Force -Path $resolvedBackupDirectory | Out-Null

if ($Action -eq "Plan") {
    Write-Host "隔离恢复演练计划（不连接服务、不执行迁移、不修改卷）"
    Write-Host "1. 备份业务 PostgreSQL、独立 Checkpointer、Keycloak PostgreSQL（如启用）。"
    Write-Host "2. 备份对象存储卷并生成 SHA-256 manifest。"
    Write-Host "3. 在新的 Compose project/volume 恢复，校验 migration head、checksum 和租户隔离。"
    Write-Host "4. 从 PostgreSQL approved/index projection facts 注册新 Milvus Collection。"
    Write-Host "5. 完整 project/verify 后，才由授权 API 显式 activate Alias。"
    Write-Host "6. 任一步骤失败，保留旧 Alias 和旧环境，不执行 destructive cleanup。"
    exit 0
}

$manifestPath = Join-Path $resolvedBackupDirectory "manifest.json"
$businessDump = Join-Path $resolvedBackupDirectory "business.dump"
$authDump = Join-Path $resolvedBackupDirectory "keycloak.dump"
$objectArchive = Join-Path $resolvedBackupDirectory "object-storage.tgz"

if ($Action -eq "Backup") {
    Invoke-DrillStep "备份业务 PostgreSQL（custom format）" {
        docker compose -p $ComposeProject --profile core exec -T postgres `
            sh -c 'pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' `
            > $businessDump
    }

    Invoke-DrillStep "备份 Keycloak PostgreSQL（如 auth profile 已启用）" {
        docker compose -p $ComposeProject --profile auth exec -T postgres-auth `
            sh -c 'pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' `
            > $authDump
    }

    Invoke-DrillStep "归档对象存储卷并生成 checksum" {
        docker run --rm `
            -v "${ComposeProject}_object_storage_data:/data:ro" `
            -v "${resolvedBackupDirectory}:/backup" `
            alpine sh -c 'tar czf /backup/object-storage.tgz -C /data . && sha256sum /backup/object-storage.tgz > /backup/object-storage.tgz.sha256'
    }

    $manifest = [ordered]@{
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        compose_project = $ComposeProject
        business_dump = [IO.Path]::GetFileName($businessDump)
        auth_dump = if (Test-Path $authDump) { [IO.Path]::GetFileName($authDump) } else { $null }
        object_archive = [IO.Path]::GetFileName($objectArchive)
        notes = @(
            "凭据仅由容器 secret/环境注入，manifest 不保存 DSN、Token 或密码。",
            "Milvus/etcd 不作为事实源；恢复后从 PostgreSQL 重建。"
        )
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 -Path $manifestPath
    Write-Host "备份 manifest：$manifestPath"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($RestoreProject)) {
    throw "Restore 必须指定隔离的 -RestoreProject，避免覆盖原 Compose project。"
}
if ($RestoreProject -eq $ComposeProject) {
    throw "RestoreProject 必须与 ComposeProject 不同。"
}
if (-not (Test-Path $businessDump)) {
    throw "缺少业务备份：$businessDump"
}

Invoke-DrillStep "在隔离 project 中恢复业务 PostgreSQL" {
    Get-Content -AsByteStream -Raw -Path $businessDump |
        docker compose -p $RestoreProject --profile core exec -T postgres `
            sh -c 'pg_restore --clean --if-exists --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

if (Test-Path $authDump) {
    Invoke-DrillStep "在隔离 project 中恢复 Keycloak PostgreSQL" {
        Get-Content -AsByteStream -Raw -Path $authDump |
            docker compose -p $RestoreProject --profile auth exec -T postgres-auth `
                sh -c 'pg_restore --clean --if-exists --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    }
}

Invoke-DrillStep "恢复对象存储卷并校验归档 checksum" {
    docker run --rm `
        -v "${RestoreProject}_object_storage_data:/data" `
        -v "${resolvedBackupDirectory}:/backup:ro" `
        alpine sh -c 'sha256sum -c /backup/object-storage.tgz.sha256 && tar xzf /backup/object-storage.tgz -C /data'
}

Write-Host "恢复后人工/受控校验："
Write-Host "- alembic current 与预期 head 一致；不回滚 migration。"
Write-Host "- StoredObject checksum、租户归属、审核事实和 CorrectionEvent 数量/抽样一致。"
Write-Host "- 从 PostgreSQL approved + reviewed + valid facts 注册新 Milvus Collection。"
Write-Host "- project/verify 全部通过后，由授权 API 显式 activate Alias；失败则保持旧 Alias。"
Write-Host "- 记录 RPO、RTO、校验结果和失败恢复步骤；不得执行 down -v。"
