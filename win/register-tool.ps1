<#
.SYNOPSIS
    Register a tool into the My Tools right-click cascading menu.

.DESCRIPTION
    Reads a tool.json manifest and registers the tool under the appropriate
    submenu(s) based on its declared Contexts. Each context maps to a submenu
    root and a path parameter (%V for folder-type, %1 for item-type).

    Supported Contexts (in tool.json):
      Folder            Right-click a folder icon
      FolderBackground  Right-click empty space inside a folder
      Desktop           Right-click empty space on the desktop
      File              Right-click any file
      Drive             Right-click a drive in My Computer

    If Contexts is omitted, defaults to ["Folder", "FolderBackground", "File"].

.PARAMETER ToolDir
    Path to a tool directory containing tool.json.

.EXAMPLE
    .\register-tool.ps1 -ToolDir .\tools\Git-Sync
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ToolDir
)

$ErrorActionPreference = "Stop"

# --- Context -> submenu + parameter mapping ---
$ContextMap = @{
    "Folder"           = @{ SubMenu = "MyToolsMenu";      Param = "%V" }
    "FolderBackground" = @{ SubMenu = "MyToolsMenu";      Param = "%V" }
    "Desktop"          = @{ SubMenu = "MyToolsMenu";      Param = "%V" }
    "File"             = @{ SubMenu = "MyToolsMenuFile";  Param = "%1" }
    "Drive"            = @{ SubMenu = "MyToolsMenuDrive"; Param = "%1" }
}

$DefaultContexts = @("Folder", "FolderBackground", "File")

# --- Resolve tool definition ---
$manifestPath = Join-Path $ToolDir "tool.json"
if (-not (Test-Path $manifestPath)) {
    throw "tool.json not found in $ToolDir"
}
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$Name        = $manifest.Name
$DisplayName = $manifest.DisplayName
$Icon        = if ($manifest.Icon) { $manifest.Icon } else { "shell32.dll,0" }
$Template    = $manifest.CommandTemplate

# --- Validate ---
if ($Name -notmatch "^[a-zA-Z0-9_-]+$") {
    throw "Tool name '$Name' must be alphanumeric (hyphens/underscores allowed)."
}
if (-not $Template) {
    throw "CommandTemplate is required in tool.json"
}

# --- Resolve contexts ---
$contexts = if ($manifest.Contexts) { @($manifest.Contexts) } else { $DefaultContexts }
$invalid = $contexts | Where-Object { -not $ContextMap.ContainsKey($_) }
if ($invalid) {
    throw "Unknown context(s): $($invalid -join ', '). Valid: $($ContextMap.Keys -join ', ')"
}

# --- Group by submenu (a tool may register in multiple submenus with different params) ---
$bySubMenu = @{}
foreach ($ctx in $contexts) {
    $sub = $ContextMap[$ctx].SubMenu
    if (-not $bySubMenu.ContainsKey($sub)) {
        $bySubMenu[$sub] = $ContextMap[$ctx].Param
    }
}

# --- Register in each submenu ---
$registered = @()
foreach ($subMenu in $bySubMenu.Keys) {
    $param = $bySubMenu[$subMenu]
    $command = $Template -replace "\{ToolDir\}", $ToolDir -replace "\{PathParam\}", $param

    $toolKey = "HKCU:\Software\Classes\$subMenu\shell\$Name"
    if (-not (Test-Path -LiteralPath $toolKey)) {
        New-Item -Path $toolKey -Force | Out-Null
    }
    Set-ItemProperty -LiteralPath $toolKey -Name "MUIVerb" -Value $DisplayName
    Set-ItemProperty -LiteralPath $toolKey -Name "Icon"    -Value $Icon

    $cmdKey = Join-Path $toolKey "command"
    if (-not (Test-Path -LiteralPath $cmdKey)) {
        New-Item -Path $cmdKey -Force | Out-Null
    }
    Set-ItemProperty -LiteralPath $cmdKey -Name "(Default)" -Value $command

    $registered += "$subMenu($param)"
}

Write-Host "[+] Registered '$DisplayName' as MyTools\$Name" -ForegroundColor Green
Write-Host "    Contexts: $($contexts -join ', ')" -ForegroundColor DarkGray
Write-Host "    Submenus: $($registered -join ', ')" -ForegroundColor DarkGray
