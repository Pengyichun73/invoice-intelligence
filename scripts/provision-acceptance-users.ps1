param(
    [string]$BaseUrl = 'http://127.0.0.1:18080',
    [string]$TenantId = 'demo-tenant'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$credentialPath = Join-Path $root 'secrets/acceptance-users.txt'
$adminPasswordPath = Join-Path $root 'secrets/keycloak_admin_password.txt'
if (-not (Test-Path -LiteralPath $adminPasswordPath -PathType Leaf)) {
    throw '缺少 Keycloak 管理员密码文件。'
}

function New-Password {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (Test-Path -LiteralPath $credentialPath -PathType Leaf) {
    $credentials = Get-Content -LiteralPath $credentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    $credentials = [pscustomobject]@{
        reviewer = [pscustomobject]@{ username = 'reviewer-a'; password = New-Password }
        governor = [pscustomobject]@{ username = 'governor-b'; password = New-Password }
    }
    [IO.File]::WriteAllText(
        $credentialPath,
        ($credentials | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )
}

$adminPassword = [IO.File]::ReadAllText($adminPasswordPath, [Text.Encoding]::UTF8).TrimEnd("`r", "`n")
$token = Invoke-RestMethod -Method Post -Uri "$BaseUrl/realms/master/protocol/openid-connect/token" `
    -ContentType 'application/x-www-form-urlencoded' `
    -Body @{ grant_type = 'password'; client_id = 'admin-cli'; username = 'admin'; password = $adminPassword }
$headers = @{ Authorization = "Bearer $($token.access_token)" }
$realmUrl = "$BaseUrl/admin/realms/invoice-acceptance"
$profile = Invoke-RestMethod -Uri "$realmUrl/users/profile" -Headers $headers
foreach ($name in @('tenant_id', 'reviewer_id')) {
    if (-not @($profile.attributes | Where-Object name -eq $name).Count) {
        $profile.attributes += [pscustomobject]@{
            name = $name
            multivalued = $false
            permissions = @{ view = @('admin'); edit = @('admin') }
        }
    }
}
Invoke-RestMethod -Method Put -Uri "$realmUrl/users/profile" -Headers $headers `
    -ContentType 'application/json' -Body ($profile | ConvertTo-Json -Depth 12) | Out-Null

foreach ($entry in @(
    @{ account = $credentials.reviewer; roles = @('invoice-extractor', 'invoice-reviewer') },
    @{ account = $credentials.governor; roles = @('memory-governor') }
)) {
    $account = $entry.account
    $username = [uri]::EscapeDataString($account.username)
    $users = @(Invoke-RestMethod -Uri "$realmUrl/users?username=$username&exact=true" -Headers $headers | ForEach-Object { $_ })
    if ($users.Count -eq 0) {
        $payload = @{
            username = $account.username
            enabled = $true
            emailVerified = $true
            email = "$($account.username)@acceptance.invalid"
            firstName = $account.username
            lastName = 'Acceptance'
            attributes = @{ tenant_id = @($TenantId); reviewer_id = @($account.username) }
        } | ConvertTo-Json -Depth 5
        Invoke-RestMethod -Method Post -Uri "$realmUrl/users" -Headers $headers `
            -ContentType 'application/json' -Body $payload | Out-Null
        $users = @(Invoke-RestMethod -Uri "$realmUrl/users?username=$username&exact=true" -Headers $headers | ForEach-Object { $_ })
    }
    if ($users.Count -ne 1) { throw "无法唯一定位账号 $($account.username)" }
    $userId = $users[0].id
    $currentUser = Invoke-RestMethod -Uri "$realmUrl/users/$userId" -Headers $headers
    $userBody = @{
        username = $account.username
        enabled = $true
        email = if ($currentUser.email) { $currentUser.email } else { "$($account.username)@acceptance.invalid" }
        emailVerified = $true
        firstName = if ($currentUser.firstName) { $currentUser.firstName } else { $account.username }
        lastName = if ($currentUser.lastName) { $currentUser.lastName } else { 'Acceptance' }
        attributes = @{ tenant_id = @($TenantId); reviewer_id = @($account.username) }
    }
    $userBody = $userBody | ConvertTo-Json -Depth 5
    Invoke-RestMethod -Method Put -Uri "$realmUrl/users/$userId" -Headers $headers `
        -ContentType 'application/json' -Body $userBody | Out-Null
    $passwordBody = @{ type = 'password'; value = $account.password; temporary = $false } | ConvertTo-Json
    Invoke-RestMethod -Method Put -Uri "$realmUrl/users/$userId/reset-password" -Headers $headers `
        -ContentType 'application/json' -Body $passwordBody | Out-Null
    foreach ($roleName in $entry.roles) {
        try {
            $role = Invoke-RestMethod -Uri "$realmUrl/roles/$roleName" -Headers $headers
        } catch {
            if ([int]$_.Exception.Response.StatusCode -ne 404) { throw }
            $roleBody = @{ name = $roleName } | ConvertTo-Json
            Invoke-RestMethod -Method Post -Uri "$realmUrl/roles" -Headers $headers `
                -ContentType 'application/json' -Body $roleBody | Out-Null
            $role = Invoke-RestMethod -Uri "$realmUrl/roles/$roleName" -Headers $headers
        }
        $roleBody = ConvertTo-Json -InputObject @($role) -Depth 5
        Invoke-RestMethod -Method Post -Uri "$realmUrl/users/$userId/role-mappings/realm" -Headers $headers `
            -ContentType 'application/json' -Body $roleBody | Out-Null
    }
}

Write-Output "已配置两个同租户、不同 reviewer_id 的验收账号。凭据仅保存在 $credentialPath，请勿提交或粘贴。"
