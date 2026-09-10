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
# Resolve to absolute path so {ToolDir} produces a stable, working command
$ToolDir = (Resolve-Path $ToolDir).Path
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
$hasSubcommands = $manifest.Subcommands -and @($manifest.Subcommands).Count -gt 0
$registered = @()

foreach ($subMenu in $bySubMenu.Keys) {
    $param = $bySubMenu[$subMenu]
    $baseCommand = $Template -replace "\{ToolDir\}", $ToolDir -replace "\{PathParam\}", $param

    if ($hasSubcommands) {
        # --- Cascading submenu: parent key has ExtendedSubCommandsKey, no command ---
        $toolKey = "HKCU:\Software\Classes\$subMenu\shell\$Name"
        if (-not (Test-Path -LiteralPath $toolKey)) {
            New-Item -Path $toolKey -Force | Out-Null
        }
        Set-ItemProperty -LiteralPath $toolKey -Name "MUIVerb" -Value $DisplayName
        Set-ItemProperty -LiteralPath $toolKey -Name "Icon"    -Value $Icon
        Set-ItemProperty -LiteralPath $toolKey -Name "ExtendedSubCommandsKey" -Value "${Name}Menu"
        # Ensure no leftover command subkey (would break cascading)
        $cmdKey = Join-Path $toolKey "command"
        if (Test-Path -LiteralPath $cmdKey) { Remove-Item -LiteralPath $cmdKey -Recurse -Force }

        # --- Create submenu root and child entries ---
        $subRoot = "HKCU:\Software\Classes\${Name}Menu\shell"
        if (-not (Test-Path -LiteralPath $subRoot)) {
            New-Item -Path $subRoot -Force | Out-Null
        }

        foreach ($sub in $manifest.Subcommands) {
            $subName = "$Name-$($sub.Name)"
            $subKey = Join-Path $subRoot $subName
            if (-not (Test-Path -LiteralPath $subKey)) {
                New-Item -Path $subKey -Force | Out-Null
            }
            Set-ItemProperty -LiteralPath $subKey -Name "MUIVerb" -Value $sub.DisplayName
            if ($sub.Icon) {
                Set-ItemProperty -LiteralPath $subKey -Name "Icon" -Value $sub.Icon
            }
            # AppliesTo: AQS predicate to filter when this submenu item is visible
            # e.g. "System.FileExtension:.py OR System.FileName:Makefile"
            if ($sub.AppliesTo) {
                Set-ItemProperty -LiteralPath $subKey -Name "AppliesTo" -Value $sub.AppliesTo
            } elseif ((Get-ItemProperty -LiteralPath $subKey -Name "AppliesTo" -ErrorAction SilentlyContinue)) {
                # Remove stale AppliesTo from a previous registration
                Remove-ItemProperty -LiteralPath $subKey -Name "AppliesTo" -ErrorAction SilentlyContinue
            }

            $subCmdKey = Join-Path $subKey "command"
            if (-not (Test-Path -LiteralPath $subCmdKey)) {
                New-Item -Path $subCmdKey -Force | Out-Null
            }
            $subCommand = $baseCommand
            if ($sub.Args) {
                $subCommand += " -ExtraArgs `"$($sub.Args)`""
            }
            Set-ItemProperty -LiteralPath $subCmdKey -Name "(Default)" -Value $subCommand
        }

        $registered += "$subMenu(submenu:$($manifest.Subcommands.Count) items)"
    }
    else {
        # --- Single command entry (existing behavior) ---
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
        Set-ItemProperty -LiteralPath $cmdKey -Name "(Default)" -Value $baseCommand

        $registered += "$subMenu($param)"
    }
}

Write-Host "[+] Registered '$DisplayName' as MyTools\$Name" -ForegroundColor Green
Write-Host "    Contexts: $($contexts -join ', ')" -ForegroundColor DarkGray
Write-Host "    Submenus: $($registered -join ', ')" -ForegroundColor DarkGray

# --- Generate CLI shim in bin\ (relative path via %~dp0 for portability) ---
$BinDir = Join-Path $PSScriptRoot "bin"
if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Path $BinDir -Force | Out-Null }

# Use the explicit Script field from tool.json (single source of truth)
$scriptName = $manifest.Script
if (-not $scriptName) {
    Write-Host "    [!] No 'Script' field in tool.json; skipping shim" -ForegroundColor Yellow
} else {
    $ps1Path = Join-Path $ToolDir $scriptName
    if (-not (Test-Path $ps1Path)) {
        Write-Host "    [!] Script not found: $ps1Path; skipping shim" -ForegroundColor Yellow
    } else {
        # Compute relative path from bin\ to the .ps1 so shims are portable.
        # [IO.Path]::GetRelativePath is not available in .NET Framework (PS 5.1), use Uri.
        $fromUri = New-Object System.Uri(($BinDir.TrimEnd('\') + '\'))
        $toUri = New-Object System.Uri($ps1Path)
        $relPath = [System.Uri]::UnescapeDataString($fromUri.MakeRelativeUri($toUri).ToString()) -replace '/', '\'
        $shimPath = Join-Path $BinDir "$Name.cmd"
        # %~dp0 = directory of this .cmd (bin\), with trailing backslash
        $shimContent = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$relPath`" %* -NoPause`r`n"
        Set-Content -Path $shimPath -Value $shimContent -Encoding ASCII
        Write-Host "    CLI shim: $shimPath (relative: $relPath)" -ForegroundColor DarkGray
    }
}
