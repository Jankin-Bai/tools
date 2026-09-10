<#
.SYNOPSIS
    Install the MyTools right-click cascading menu framework.

.DESCRIPTION
    Registers a "My Tools" cascading submenu under the folder right-click context
    menu. All tools found under .\tools\ that have a tool.json manifest are
    auto-registered. Sysinternals handle.exe is downloaded to .\bin\ if missing.

.NOTES
    Per-user registration (HKCU\Software\Classes). No admin rights required.
    Windows 10: menu appears directly. Windows 11: via "Show more options".
#>

[CmdletBinding()]
param(
    [string]$ToolsRoot = "",
    [string]$BinDir   = ""
)

$ErrorActionPreference = "Stop"

# Resolve defaults after script start (PSScriptRoot is not available in param defaults)
if (-not $ToolsRoot) { $ToolsRoot = Join-Path $PSScriptRoot "tools" }
if (-not $BinDir)   { $BinDir   = Join-Path $PSScriptRoot "bin" }

# --- Registry paths (per-user, maps to HKCR) ---
$ParentKey   = "HKCU:\Software\Classes\Directory\shell\MyTools"
$MenuRootKey = "HKCU:\Software\Classes\MyToolsMenu\shell"

function Write-Step {
    param([string]$Message)
    Write-Host "[*] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[+] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[!] $Message" -ForegroundColor Yellow
}

# --- 1. Create parent cascading menu ---
Write-Step "Registering My Tools cascading menu..."

if (-not (Test-Path $ParentKey)) {
    New-Item -Path $ParentKey -Force | Out-Null
}
Set-ItemProperty -Path $ParentKey -Name "MUIVerb"               -Value "My Tools"
Set-ItemProperty -Path $ParentKey -Name "ExtendedSubCommandsKey" -Value "MyToolsMenu"
Set-ItemProperty -Path $ParentKey -Name "Icon"                  -Value "shell32.dll,14"
# Remove any legacy command subkey so the parent stays a pure container
$cmdSub = Join-Path $ParentKey "command"
if (Test-Path $cmdSub) { Remove-Item $cmdSub -Recurse -Force }

if (-not (Test-Path $MenuRootKey)) {
    New-Item -Path $MenuRootKey -Force | Out-Null
}
Write-Ok "Parent menu registered at HKCU\Software\Classes\Directory\shell\MyTools"

# --- 2. Ensure bin directory exists ---
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

# --- 3. Download handle.exe if missing ---
$handleExe = Join-Path $BinDir "handle64.exe"
if ([Environment]::Is64BitOperatingSystem) {
    $handleExe = Join-Path $BinDir "handle64.exe"
} else {
    $handleExe = Join-Path $BinDir "handle.exe"
}

if (-not (Test-Path $handleExe)) {
    Write-Step "Downloading Sysinternals handle.exe..."
    $zipUrl  = "https://download.sysinternals.com/files/Handle.zip"
    $zipPath = Join-Path $BinDir "Handle.zip"
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing -ErrorAction Stop
        Expand-Archive -Path $zipPath -DestinationPath $BinDir -Force
        Remove-Item $zipPath -Force
        Write-Ok "handle.exe downloaded to $BinDir"
    } catch {
        Write-Warn "Failed to download handle.exe: $_"
        Write-Warn "Tools requiring handle.exe will prompt for download at runtime."
    }
} else {
    Write-Ok "handle.exe already present: $handleExe"
}

# --- 4. Auto-register all tools with tool.json manifests ---
Write-Step "Registering tools from $ToolsRoot ..."

if (Test-Path $ToolsRoot) {
    $manifests = Get-ChildItem -Path $ToolsRoot -Recurse -Filter "tool.json" -ErrorAction SilentlyContinue
    $registerScript = Join-Path $PSScriptRoot "register-tool.ps1"

    foreach ($manifest in $manifests) {
        $toolDir = Split-Path $manifest.FullName -Parent
        Write-Step "  Registering: $($manifest.Directory.Name)"
        try {
            & $registerScript -ToolDir $toolDir -ErrorAction Stop
            Write-Ok "    -> registered"
        } catch {
            Write-Warn "    -> failed: $_"
        }
    }
} else {
    Write-Warn "Tools directory not found: $ToolsRoot"
}

Write-Host ""
Write-Ok "MyTools framework installed successfully."
Write-Host "    Right-click any folder -> My Tools to access registered tools."
Write-Host "    Add new tools by placing them under .\tools\<Name>\ with a tool.json,"
Write-Host "    then run: .\register-tool.ps1 -ToolDir .\tools\<Name>"
