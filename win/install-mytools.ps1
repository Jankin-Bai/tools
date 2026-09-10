<#
.SYNOPSIS
    Install the MyTools right-click cascading menu framework.

.DESCRIPTION
    Registers "My Tools" cascading menus for multiple context scenarios:
    Folder, Folder background, File, Drive, Desktop.
    Auto-registers every tool found under tools\ based on its tool.json Contexts.

.PARAMETER ToolsRoot
    Path to the tools directory. Defaults to .\tools
.PARAMETER BinDir
    Path to the bin directory for handle.exe. Defaults to .\bin
#>

[CmdletBinding()]
param(
    [string]$ToolsRoot,
    [string]$BinDir
)

$ErrorActionPreference = "Stop"

if (-not $ToolsRoot) { $ToolsRoot = Join-Path $PSScriptRoot "tools" }
if (-not $BinDir)   { $BinDir   = Join-Path $PSScriptRoot "bin" }

# --- Context definitions: entry point -> submenu root ---
$EntryPoints = @(
    @{ Class = "Directory";            SubMenu = "MyToolsMenu";     Param = "%V"; Label = "Folder" }
    @{ Class = "Directory\Background"; SubMenu = "MyToolsMenu";     Param = "%V"; Label = "Folder background" }
    @{ Class = "DesktopBackground";    SubMenu = "MyToolsMenu";     Param = "%V"; Label = "Desktop" }
    @{ Class = "*";                    SubMenu = "MyToolsMenuFile"; Param = "%1"; Label = "File" }
    @{ Class = "Drive";                SubMenu = "MyToolsMenuDrive"; Param = "%1"; Label = "Drive" }
)

function Write-Step { param([string]$m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "[!] $m" -ForegroundColor Yellow }

function Register-EntryPoint {
    param([string]$Class, [string]$SubMenu, [string]$Label)
    $keyPath = "HKCU:\Software\Classes\$Class\shell\MyTools"
    # New-Item has no -LiteralPath in PS 5.1; use .NET for paths containing *
    $regPath = "Software\Classes\$Class\shell\MyTools" -replace "\\+", "\"
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($regPath)
    $key.Close()
    Set-ItemProperty -LiteralPath $keyPath -Name "MUIVerb"               -Value "My Tools"
    Set-ItemProperty -LiteralPath $keyPath -Name "ExtendedSubCommandsKey" -Value $SubMenu
    Set-ItemProperty -LiteralPath $keyPath -Name "Icon"                  -Value "shell32.dll,14"
    $cmdSub = Join-Path $keyPath "command"
    if (Test-Path -LiteralPath $cmdSub) { Remove-Item -LiteralPath $cmdSub -Recurse -Force }
    Write-Ok "$Label menu -> $SubMenu"
}

# --- 1. Register all entry points ---
Write-Step "Registering My Tools entry points..."
foreach ($ep in $EntryPoints) {
    Register-EntryPoint -Class $ep.Class -SubMenu $ep.SubMenu -Label $ep.Label
}

# --- 2. Create submenu roots ---
foreach ($sub in @("MyToolsMenu","MyToolsMenuFile","MyToolsMenuDrive")) {
    $root = "HKCU:\Software\Classes\$sub\shell"
    if (-not (Test-Path -LiteralPath $root)) {
        New-Item -Path $root -Force | Out-Null
    }
}
Write-Ok "Submenu roots created"

# --- 3. Ensure bin directory and handle.exe ---
if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Path $BinDir -Force | Out-Null }
$handleExe = Join-Path $BinDir "handle64.exe"
if (-not (Test-Path $handleExe)) {
    $handleExe = Join-Path $BinDir "handle.exe"
}
if (Test-Path $handleExe) {
    Write-Ok "handle.exe present: $handleExe"
} else {
    Write-Warn "handle.exe not found in $BinDir. Download from Sysinternals."
}

# --- 4. Auto-register all tools ---
Write-Step "Registering tools from $ToolsRoot ..."
if (Test-Path $ToolsRoot) {
    Get-ChildItem $ToolsRoot -Directory | ForEach-Object {
        $manifest = Join-Path $_.FullName "tool.json"
        if (Test-Path $manifest) {
            Write-Host "  Registering: $($_.Name)" -ForegroundColor DarkGray
            & (Join-Path $PSScriptRoot "register-tool.ps1") -ToolDir $_.FullName
        }
    }
}

Write-Host ""
Write-Host "[+] MyTools framework installed successfully." -ForegroundColor Green
Write-Host "    Right-click scenarios: Folder | Folder background | File | Drive | Desktop"
Write-Host "    Add new tools: create tools\<Name>\ with tool.json, then run .\register-tool.ps1"
