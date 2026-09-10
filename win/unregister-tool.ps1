<#
.SYNOPSIS
    Unregister a tool from all My Tools submenus.

.DESCRIPTION
    Removes the tool's registry entries from MyToolsMenu, MyToolsMenuFile,
    and MyToolsMenuDrive. The tool directory and scripts are left intact.

.PARAMETER Name
    The tool's unique registry key name (from tool.json "Name" field).

.EXAMPLE
    .\unregister-tool.ps1 -Name gitsync
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Name
)

$ErrorActionPreference = "Stop"

$SubMenus = @("MyToolsMenu", "MyToolsMenuFile", "MyToolsMenuDrive")
$removed = @()

foreach ($sub in $SubMenus) {
    $key = "HKCU:\Software\Classes\$sub\shell\$Name"
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
        $removed += $sub
    }
}

if ($removed.Count -gt 0) {
    Write-Host "[+] Unregistered '$Name' from: $($removed -join ', ')" -ForegroundColor Green
} else {
    Write-Host "[!] Tool '$Name' not found in any submenu." -ForegroundColor Yellow
}
