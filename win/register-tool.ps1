<#
.SYNOPSIS
    Register a tool into the My Tools right-click cascading menu.

.DESCRIPTION
    Reads a tool.json manifest from the specified tool directory and creates the
    corresponding registry entry under HKCU\Software\Classes\MyToolsMenu\shell\.
    Can also register with explicit parameters without a manifest.

.PARAMETER ToolDir
    Path to a tool directory containing tool.json.

.PARAMETER Name
    Unique registry key name for the tool (alphanumeric, no spaces).

.PARAMETER DisplayName
    Text shown in the context menu.

.PARAMETER Icon
    Icon reference, e.g. "shell32.dll,47" or a full .ico/.exe path.

.PARAMETER Command
    Full command line executed when the menu item is clicked. Use "%1" for the
    selected folder path.

.EXAMPLE
    .\register-tool.ps1 -ToolDir .\tools\Unlock-Folder

.EXAMPLE
    .\register-tool.ps1 -Name "mytool" -DisplayName "Do Thing" -Icon "shell32.dll,5" `
        -Command 'powershell.exe -NoProfile -File "C:\tool.ps1" -Path "%1"'
#>

[CmdletBinding(DefaultParameterSetName = "Manifest")]
param(
    [Parameter(ParameterSetName = "Manifest", Mandatory = $true)]
    [string]$ToolDir,

    [Parameter(ParameterSetName = "Explicit", Mandatory = $true)]
    [string]$Name,

    [Parameter(ParameterSetName = "Explicit", Mandatory = $true)]
    [string]$DisplayName,

    [Parameter(ParameterSetName = "Explicit")]
    [string]$Icon = "shell32.dll,0",

    [Parameter(ParameterSetName = "Explicit", Mandatory = $true)]
    [string]$Command
)

$ErrorActionPreference = "Stop"
$MenuRootKey = "HKCU:\Software\Classes\MyToolsMenu\shell"

# --- Resolve tool definition ---
if ($PSCmdlet.ParameterSetName -eq "Manifest") {
    $manifestPath = Join-Path $ToolDir "tool.json"
    if (-not (Test-Path $manifestPath)) {
        throw "tool.json not found in $ToolDir"
    }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $Name        = $manifest.Name
    $DisplayName = $manifest.DisplayName
    $Icon        = if ($manifest.Icon) { $manifest.Icon } else { "shell32.dll,0" }
    $Command     = $manifest.CommandTemplate -replace "\{ToolDir\}", $ToolDir
}

# --- Validate name ---
if ($Name -notmatch "^[a-zA-Z0-9_-]+$") {
    throw "Tool name '$Name' must be alphanumeric (hyphens/underscores allowed)."
}

# --- Create registry entry ---
$toolKey = Join-Path $MenuRootKey $Name
if (-not (Test-Path $toolKey)) {
    New-Item -Path $toolKey -Force | Out-Null
}
Set-ItemProperty -Path $toolKey -Name "MUIVerb" -Value $DisplayName
Set-ItemProperty -Path $toolKey -Name "Icon"    -Value $Icon

$cmdKey = Join-Path $toolKey "command"
if (-not (Test-Path $cmdKey)) {
    New-Item -Path $cmdKey -Force | Out-Null
}
Set-ItemProperty -Path $cmdKey -Name "(Default)" -Value $Command

Write-Host "[+] Registered '$DisplayName' as MyTools\$Name" -ForegroundColor Green
