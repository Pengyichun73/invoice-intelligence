[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Backup', 'Restore')][string]$Action = 'Plan',
    [string]$ComposeProject = 'invoice-longrun-acceptance-20260925',
    [string]$ComposeFile = '.\artifacts\longrun-acceptance-20260925\compose.yml',
    [string]$BackupDirectory = '.\artifacts\backup-drill',
    [string]$RestoreProject = '',
    [string]$RestoreOverrideFile = '',
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$expectedDatabase = 'invoice_longrun_acceptance'

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker 命令失败：$($Arguments[0]) $($Arguments[1])" }
}

function Get-DockerText {
    param([string[]]$Arguments)
    $value = & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker 读取失败：$($Arguments[0]) $($Arguments[1])" }
    return ($value | Out-String).Trim()
}

function Assert-Project {
    param([string]$Name, [string]$Prefix)
    if (-not $Name.StartsWith($Prefix, [StringComparison]::Ordinal) -or
        $Name -eq 'invoice-intelligence' -or $Name -match '[^a-z0-9-]') {
        throw "拒绝非隔离 Compose project：$Name"
    }
}

function Get-ServiceContainer {
    param([string]$Project, [string]$Service, [string[]]$Files, [switch]$Optional)
    $id = & docker compose @Files -p $Project ps -a -q $Service 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($id | Out-String))) {
        if ($Optional) { return $null }
        throw "隔离 project 缺少服务：$Project/$Service"
    }
    $id = ($id | Out-String).Trim()
    $owner = Get-DockerText -Arguments @('inspect', $id, '--format',
        '{{index .Config.Labels "com.docker.compose.project"}}')
    if ($owner -ne $Project) { throw "容器归属不符：$Service" }
    return $id
}

function Get-ProjectVolume {
    param([string]$Project, [string]$Service, [string]$Target, [string[]]$Files)
    $id = Get-ServiceContainer $Project $Service $Files
    $mounts = Get-DockerText -Arguments @('inspect', $id, '--format', '{{json .Mounts}}') |
        ConvertFrom-Json
    $mount = @($mounts | Where-Object { $_.Type -eq 'volume' -and $_.Destination -eq $Target })
    if ($mount.Count -ne 1) { throw "卷挂载不唯一：$Service/$Target" }
    $volume = $mount[0].Name
    $labels = Get-DockerText -Arguments @('volume', 'inspect', $volume, '--format',
        '{{json .Labels}}') | ConvertFrom-Json
    if ($labels.'com.docker.compose.project' -ne $Project) { throw "卷归属不符：$volume" }
    return $volume
}

function Save-Volume {
    param([string]$Volume, [string]$Name, [string]$Directory)
    Invoke-Docker -Arguments @('run', '--rm',
        '--mount', "type=volume,source=$Volume,target=/source,readonly",
        '--mount', "type=bind,source=$Directory,target=/backup",
        'busybox:latest', 'tar', '-czf', "/backup/$Name", '-C', '/source', '.')
}

function Restore-Volume {
    param([string]$Volume, [string]$Name, [string]$Directory)
    Invoke-Docker -Arguments @('run', '--rm',
        '--mount', "type=volume,source=$Volume,target=/target",
        '--mount', "type=bind,source=$Directory,target=/backup,readonly",
        'busybox:latest', 'sh', '-c',
        "if find /target -mindepth 1 -maxdepth 1 | grep -q .; then exit 40; fi; tar -xzf /backup/$Name -C /target")
}

function Save-Database {
    param([string]$Container, [string]$Path, [string]$Database, [string]$User)
    $tmp = '/tmp/isolated-restore-drill.dump'
    try {
        Invoke-Docker -Arguments @('exec', $Container, 'pg_dump', '--format=custom',
            '--no-owner', '--no-privileges', '-U', $User,
            '-d', $Database, '-f', $tmp)
        Invoke-Docker -Arguments @('cp', "${Container}:$tmp", $Path)
    } finally {
        & docker exec $Container rm -f $tmp *> $null
    }
}

function Restore-Database {
    param([string]$Container, [string]$Path, [string]$Database, [string]$User)
    $tmp = '/tmp/isolated-restore-drill.dump'
    try {
        Invoke-Docker -Arguments @('cp', $Path, "${Container}:$tmp")
        $null = Get-DockerText -Arguments @('exec', $Container, 'pg_restore', '--list', $tmp)
        $count = Get-DockerText -Arguments @('exec', $Container, 'psql',
            '-U', $User, '-d', $Database, '-At', '-c',
            "select count(*) from information_schema.tables where table_schema='public'")
        if ($count -ne '0') { throw "目标数据库非空，拒绝覆盖：$Database" }
        Invoke-Docker -Arguments @('exec', $Container, 'pg_restore', '--exit-on-error',
            '--no-owner', '--no-privileges', '-U', $User,
            '-d', $Database, $tmp)
    } finally {
        & docker exec $Container rm -f $tmp *> $null
    }
}

if ($Action -eq 'Plan') {
    Write-Output '隔离计划：核对标签与目标空卷；冻结源写入；备份业务库、实际 Checkpointer、可选认证库及对象卷；校验 SHA-256；恢复到全新 project；核对事实与派生索引。'
    return
}
if (-not $Execute) { throw 'Backup/Restore 必须显式提供 -Execute。' }
Assert-Project $ComposeProject 'invoice-longrun-acceptance-'
$resolvedComposeFile = [IO.Path]::GetFullPath($ComposeFile)
if (-not (Test-Path -LiteralPath $resolvedComposeFile -PathType Leaf)) {
    throw 'Compose 文件不存在。'
}
$sourceFiles = @('-f', $resolvedComposeFile)
$directory = [IO.Path]::GetFullPath($BackupDirectory)
$manifestPath = Join-Path $directory 'manifest.json'

if ($Action -eq 'Backup') {
    if (Test-Path -LiteralPath $manifestPath) { throw '备份目录已含 manifest，拒绝覆盖。' }
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $business = Get-ServiceContainer $ComposeProject 'postgres' $sourceFiles
    $api = Get-ServiceContainer $ComposeProject 'api' $sourceFiles
    $backend = Get-DockerText -Arguments @('exec', $api, 'printenv',
        'INVOICE_INTELLIGENCE_CHECKPOINT_BACKEND')
    $database = Get-DockerText -Arguments @('exec', $business, 'printenv', 'POSTGRES_DB')
    if ($database -ne $expectedDatabase) { throw '业务数据库不是隔离验收库。' }
    $checkpoint = $null
    if ($backend -eq 'postgres') {
        $checkpoint = Get-ServiceContainer $ComposeProject 'postgres-checkpoint' $sourceFiles
    } elseif ($backend -ne 'sqlite') {
        throw "不支持的 Checkpointer：$backend"
    }
    $auth = Get-ServiceContainer $ComposeProject 'postgres-auth' $sourceFiles -Optional
    $objectVolume = Get-ProjectVolume $ComposeProject 'minio' '/data' $sourceFiles
    $checkpointVolume = if ($backend -eq 'sqlite') {
        Get-ProjectVolume $ComposeProject 'api' '/app/.data' $sourceFiles
    } else { $null }
    $head = Get-DockerText -Arguments @('exec', $business, 'psql',
        '-U', 'invoice_intelligence', '-d', $expectedDatabase, '-At',
        '-c', 'select version_num from alembic_version')
    $frozenAt = [DateTime]::UtcNow
    try {
        Invoke-Docker -Arguments (@('compose') + $sourceFiles + @('-p', $ComposeProject,
            'stop', 'api', 'index-worker', 'milvus', 'minio'))
        Save-Database $business (Join-Path $directory 'business.dump') $expectedDatabase 'invoice_intelligence'
        if ($checkpoint) {
            $checkpointDb = Get-DockerText -Arguments @('exec', $checkpoint,
                'printenv', 'POSTGRES_DB')
            $checkpointUser = Get-DockerText -Arguments @('exec', $checkpoint,
                'printenv', 'POSTGRES_USER')
            Save-Database $checkpoint (Join-Path $directory 'checkpoint.dump') $checkpointDb $checkpointUser
        } else {
            Invoke-Docker -Arguments @('run', '--rm', '--mount',
                "type=volume,source=$checkpointVolume,target=/source,readonly",
                'busybox:latest', 'test', '-s', '/source/checkpoints.sqlite')
            Save-Volume $checkpointVolume 'checkpoint-volume.tgz' $directory
        }
        if ($auth) {
            $authDb = Get-DockerText -Arguments @('exec', $auth, 'printenv', 'POSTGRES_DB')
            $authUser = Get-DockerText -Arguments @('exec', $auth, 'printenv', 'POSTGRES_USER')
            Save-Database $auth (Join-Path $directory 'keycloak.dump') $authDb $authUser
        }
        Save-Volume $objectVolume 'object-storage.tgz' $directory
    } finally {
        Invoke-Docker -Arguments (@('compose') + $sourceFiles + @('-p', $ComposeProject,
            'start', 'minio', 'milvus', 'api', 'index-worker'))
    }
    $names = @('business.dump', 'object-storage.tgz')
    $names += if ($checkpoint) { 'checkpoint.dump' } else { 'checkpoint-volume.tgz' }
    if ($auth) { $names += 'keycloak.dump' }
    $hashes = [ordered]@{}
    foreach ($name in $names) {
        $filePath = Join-Path $directory $name
        $hashes[$name] = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash
    }
    [ordered]@{
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        frozen_at_utc = $frozenAt.ToString('o')
        compose_project = $ComposeProject
        business_database = $expectedDatabase
        alembic_head = $head
        checkpoint_backend = $backend
        auth_enabled = [bool]$auth
        files_sha256 = $hashes
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath $manifestPath
    Write-Output "隔离备份完成：$manifestPath"
    return
}

Assert-Project $RestoreProject 'invoice-restore-'
if (-not $RestoreOverrideFile) { throw 'Restore 必须提供独立端口覆盖文件。' }
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw '缺少备份 manifest。'
}
$manifest = Get-Content -Raw -Encoding utf8 -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.compose_project -ne $ComposeProject -or
    $manifest.business_database -ne $expectedDatabase) {
    throw '备份来源不匹配。'
}
foreach ($entry in $manifest.files_sha256.PSObject.Properties) {
    $file = Join-Path $directory $entry.Name
    if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash -ne $entry.Value) {
        throw "备份 checksum 不匹配：$($entry.Name)"
    }
}
$existing = Get-DockerText -Arguments @('ps', '-aq', '--filter',
    "label=com.docker.compose.project=$RestoreProject")
$existingVolumes = Get-DockerText -Arguments @('volume', 'ls', '-q', '--filter',
    "label=com.docker.compose.project=$RestoreProject")
if ($existing -or $existingVolumes) { throw '目标 project 或卷已存在，拒绝覆盖。' }
$restoreFiles = $sourceFiles
if ($RestoreOverrideFile) {
    $override = [IO.Path]::GetFullPath($RestoreOverrideFile)
    if (-not (Test-Path -LiteralPath $override -PathType Leaf)) { throw '恢复覆盖文件不存在。' }
    $restoreFiles += @('-f', $override)
}
$restoreStart = [DateTime]::UtcNow
Invoke-Docker -Arguments (@('compose') + $restoreFiles + @('-p', $RestoreProject,
    'up', '-d', '--wait', 'postgres'))
Invoke-Docker -Arguments (@('compose') + $restoreFiles + @('-p', $RestoreProject,
    'create', 'minio', 'api'))
$targetBusiness = Get-ServiceContainer $RestoreProject 'postgres' $restoreFiles
$targetDb = Get-DockerText -Arguments @('exec', $targetBusiness, 'printenv', 'POSTGRES_DB')
if ($targetDb -ne $expectedDatabase) { throw '目标数据库不是隔离验收库。' }
Restore-Database $targetBusiness (Join-Path $directory 'business.dump') $expectedDatabase 'invoice_intelligence'
if ($manifest.checkpoint_backend -eq 'sqlite') {
    $volume = Get-ProjectVolume $RestoreProject 'api' '/app/.data' $restoreFiles
    Restore-Volume $volume 'checkpoint-volume.tgz' $directory
} elseif ($manifest.checkpoint_backend -eq 'postgres') {
    Invoke-Docker -Arguments (@('compose') + $restoreFiles + @('-p', $RestoreProject,
        'up', '-d', '--wait', 'postgres-checkpoint'))
    $target = Get-ServiceContainer $RestoreProject 'postgres-checkpoint' $restoreFiles
    $db = Get-DockerText -Arguments @('exec', $target, 'printenv', 'POSTGRES_DB')
    $user = Get-DockerText -Arguments @('exec', $target, 'printenv', 'POSTGRES_USER')
    Restore-Database $target (Join-Path $directory 'checkpoint.dump') $db $user
} else { throw '未知 Checkpointer backend。' }
if ($manifest.auth_enabled) {
    Invoke-Docker -Arguments (@('compose') + $restoreFiles + @('-p', $RestoreProject,
        'up', '-d', '--wait', 'postgres-auth'))
    $target = Get-ServiceContainer $RestoreProject 'postgres-auth' $restoreFiles
    $db = Get-DockerText -Arguments @('exec', $target, 'printenv', 'POSTGRES_DB')
    $user = Get-DockerText -Arguments @('exec', $target, 'printenv', 'POSTGRES_USER')
    Restore-Database $target (Join-Path $directory 'keycloak.dump') $db $user
}
$volume = Get-ProjectVolume $RestoreProject 'minio' '/data' $restoreFiles
Restore-Volume $volume 'object-storage.tgz' $directory
Invoke-Docker -Arguments (@('compose') + $restoreFiles + @('-p', $RestoreProject,
    'up', '-d', '--wait'))
$restoredHead = Get-DockerText -Arguments @('exec', $targetBusiness, 'psql',
    '-U', 'invoice_intelligence', '-d', $expectedDatabase, '-At',
    '-c', 'select version_num from alembic_version')
if ($restoredHead -ne $manifest.alembic_head) { throw '恢复后 Alembic head 不匹配。' }
$rto = ([DateTime]::UtcNow - $restoreStart).TotalSeconds
Write-Output "隔离恢复完成：project=$RestoreProject head=$restoredHead RTO_seconds=$([math]::Round($rto, 2))"
