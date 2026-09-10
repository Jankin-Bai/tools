<#
.SYNOPSIS
    Remove a tool from the My Tools right-click cascading menu.

.PARAMETER Name
    The unique tool name (registry key name under MyToolsMenu\shell).

.EXAMPLE
    .\unregister-tool.ps1 -Name unlockfolder
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Name
)

$ErrorActionPreference = "Stop"
$toolKey = "HKCU:\Software\Classes\MyToolsMenu\shell\$Name"

if (Test-Path $toolKey) {
    Remove-Item -Path $toolKey -Recurse -Force
    Write-Host "[+] Unregistered tool: $Name" -ForegroundColor Green
} else {
    Write-Host "[!] Tool not found: $Name" -ForegroundColor Yellow
}
