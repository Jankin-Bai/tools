<#
.SYNOPSIS
    Uninstall the MyTools right-click cascading menu framework.

.DESCRIPTION
    Removes all registry entries for all My Tools entry points and submenus.
    Script files under the installation directory are NOT deleted by default.

.PARAMETER RemoveFiles
    Also delete the framework script files and tools directory.
#>

[CmdletBinding()]
param(
    [switch]$RemoveFiles
)

$ErrorActionPreference = "Stop"

$EntryClasses = @(
    "Directory\shell\MyTools",
    "Directory\Background\shell\MyTools",
    "DesktopBackground\shell\MyTools",
    "*\shell\MyTools",
    "Drive\shell\MyTools"
)
$SubMenus = @("MyToolsMenu", "MyToolsMenuFile", "MyToolsMenuDrive")

Write-Host "[*] Removing My Tools registry entries..." -ForegroundColor Cyan

foreach ($class in $EntryClasses) {
    $key = "HKCU:\Software\Classes\$class"
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
        Write-Host "[+] Removed: $class" -ForegroundColor Green
    }
}

foreach ($sub in $SubMenus) {
    $key = "HKCU:\Software\Classes\$sub"
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
        Write-Host "[+] Removed: $sub" -ForegroundColor Green
    }
}

# --- Remove bin\ from user PATH ---
$BinDir = Join-Path $PSScriptRoot "bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -like "*$BinDir*") {
    $cleaned = ($userPath -split ';' | Where-Object { $_ -and $_ -ne $BinDir }) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $cleaned, "User")
    Write-Host "[+] Removed from user PATH: $BinDir" -ForegroundColor Green
}

if ($RemoveFiles) {
    $base = $PSScriptRoot
    Write-Host "[*] Removing framework files under $base ..." -ForegroundColor Cyan
    Get-ChildItem $base -File | Where-Object { $_.Name -match "\.(ps1|json|md)$" } | ForEach-Object {
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
