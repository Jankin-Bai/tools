<#
.SYNOPSIS
    Uninstall the MyTools right-click cascading menu framework.

.DESCRIPTION
    Removes all registry entries for the My Tools menu and registered sub-tools.
    Script files under the installation directory are NOT deleted by default.

.PARAMETER RemoveFiles
    Also delete the framework script files and tools directory.
#>

[CmdletBinding()]
param(
    [switch]$RemoveFiles
)

$ErrorActionPreference = "Stop"

$FolderParentKey = "HKCU:\Software\Classes\Directory\shell\MyTools"
$FileParentKey   = "HKCU:\Software\Classes\*\shell\MyTools"
$MenuRootKey     = "HKCU:\Software\Classes\MyToolsMenu"

Write-Host "[*] Removing My Tools registry entries..." -ForegroundColor Cyan

foreach ($key in @($FolderParentKey, $FileParentKey, $MenuRootKey)) {
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
        Write-Host "[+] Removed: $key" -ForegroundColor Green
    }
}

if ($RemoveFiles) {
    $base = $PSScriptRoot
    Write-Host "[*] Removing framework files under $base ..." -ForegroundColor Cyan
    Get-ChildItem $base -File | Where-Object { $_.Name -match "\.(ps1|json)$" } | ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Host "[+] Deleted: $($_.Name)" -ForegroundColor Green
    }
    if (Test-Path "$base\tools") {
        Remove-Item "$base\tools" -Recurse -Force
        Write-Host "[+] Deleted: tools\" -ForegroundColor Green
    }
    if (Test-Path "$base\bin") {
        Remove-Item "$base\bin" -Recurse -Force
        Write-Host "[+] Deleted: bin\" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "[+] MyTools uninstalled." -ForegroundColor Green
