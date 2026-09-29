[CmdletBinding()]
param(
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start',
    [switch]$Build,
    [switch]$IncludeFrontend
)

& (Join-Path $PSScriptRoot 'manage-intelligence.ps1') -Action $Action -Build:$Build -IncludeFrontend:$IncludeFrontend
