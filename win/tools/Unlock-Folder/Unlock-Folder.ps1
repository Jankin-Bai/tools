<#
.SYNOPSIS
    Find and terminate processes that are locking a folder.

.DESCRIPTION
    Launched from the "My Tools > Unlock Folder" right-click context menu.
    Uses Sysinternals handle.exe to enumerate processes with open handles to
    the target folder (or files inside it), presents a selection grid, and
    terminates the user-chosen processes.

.PARAMETER Path
    Full path of the folder to unlock. Passed automatically by the context
    menu as "%1".

.NOTES
    handle.exe is downloaded automatically to ..\..\bin\ if missing.
    Admin rights are recommended to see handles from all processes.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

# --- Resolve paths ---
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$BinDir    = Join-Path $ScriptDir "..\..\bin"
$BinDir    = (Resolve-Path $BinDir).Path

# --- Helper: show a message box ---
function Show-Message {
    param(
        [string]$Text,
        [string]$Title = "Unlock Folder",
        [System.Windows.Forms.MessageBoxButtons]$Buttons = [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]$Icon = [System.Windows.Forms.MessageBoxIcon]::Information
    )
    Add-Type -AssemblyName System.Windows.Forms
    return [System.Windows.Forms.MessageBox]::Show($Text, $Title, $Buttons, $Icon)
}

# --- Helper: ensure handle.exe is available ---
function Get-HandleExe {
    $exeName = if ([Environment]::Is64BitOperatingSystem) { "handle64.exe" } else { "handle.exe" }
    $exePath = Join-Path $BinDir $exeName

    if (-not (Test-Path $exePath)) {
        # Fallback: try 32-bit handle.exe
        $exePath = Join-Path $BinDir "handle.exe"
    }

    if (-not (Test-Path $exePath)) {
        $result = Show-Message -Text "Sysinternals handle.exe was not found.`n`nDownload it now? (~1 MB)" -Buttons YesNo -Icon Question
        if ($result -ne "Yes") { return $null }

        if (-not (Test-Path $BinDir)) {
            New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
        }

        $zipUrl  = "https://download.sysinternals.com/files/Handle.zip"
        $zipPath = Join-Path $BinDir "Handle.zip"
        try {
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing -ErrorAction Stop
            Expand-Archive -Path $zipPath -DestinationPath $BinDir -Force
            Remove-Item $zipPath -Force
        } catch {
            Show-Message -Text "Download failed: $_`n`nPlease manually download handle.exe from`nhttps://learn.microsoft.com/sysinternals/downloads/handle`nand place it in:`n$BinDir" -Icon Error
            return $null
        }

        $exeName = if ([Environment]::Is64BitOperatingSystem) { "handle64.exe" } else { "handle.exe" }
        $exePath = Join-Path $BinDir $exeName
        if (-not (Test-Path $exePath)) { $exePath = Join-Path $BinDir "handle.exe" }
    }

    # Accept EULA silently
    $eulaKey = "HKCU:\Software\Sysinternals\Handle"
    if (-not (Test-Path $eulaKey)) { New-Item -Path $eulaKey -Force | Out-Null }
    Set-ItemProperty -Path $eulaKey -Name "EulaAccepted" -Value 1 -Type DWord

    return $exePath
}

# --- Helper: parse handle.exe output ---
# handle.exe <path> produces single-line records:
#   "explorer.exe  pid: 1234  type: File  48: C:\path\to\folder"
function Parse-HandleOutput {
    param([string[]]$Lines)

    $results = @()

    foreach ($line in $Lines) {
        if ($line -match '^(\S+\.exe)\s+pid:\s+(\d+)\s+type:\s+(\S+)\s+([0-9A-Fa-f]+):\s+(.+)$') {
            $results += [PSCustomObject]@{
                ProcessName = $Matches[1]
                PID         = [int]$Matches[2]
                HandleType  = $Matches[3]
                HandleId    = $Matches[4]
                TargetPath  = $Matches[5].Trim()
            }
        }
    }
    return $results
}

# --- Helper: check admin rights ---
function Test-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --- Main ---

# Validate path
if (-not (Test-Path $Path)) {
    Show-Message -Text "Path not found:`n$Path" -Icon Error
    exit 1
}

$item = Get-Item $Path -ErrorAction SilentlyContinue
if (-not $item.PSIsContainer) {
    Show-Message -Text "This tool works on folders only.`nSelected item is a file:`n$Path" -Icon Warning
    exit 1
}

# Admin warning
$isAdmin = Test-Admin
if (-not $isAdmin) {
    Show-Message -Text "Not running as Administrator.`nHandles from elevated or other-user processes may not be visible.`n`nFor full results, re-run from an elevated context." -Icon Warning
}

# Get handle.exe
$handleExe = Get-HandleExe
if (-not $handleExe) { exit 1 }

Write-Host "Scanning for handles to: $Path" -ForegroundColor Cyan

# Run handle.exe
# -accepteula : suppress EULA dialog
# -nobanner   : suppress copyright banner
$handleArgs = @("-accepteula", "-nobanner", $Path)
try {
    $output = & $handleExe @handleArgs 2>&1
    $exitCode = $LASTEXITCODE
} catch {
    Show-Message -Text "Failed to run handle.exe:`n$_" -Icon Error
    exit 1
}

# Parse results
$handles = Parse-HandleOutput -Lines $output

# Deduplicate by PID (one process may hold multiple handles under the folder)
$uniqueProcesses = $handles | Group-Object PID | ForEach-Object {
    $first = $_.Group[0]
    $paths = ($_.Group | Select-Object -ExpandProperty TargetPath -Unique) -join "`n  "
    [PSCustomObject]@{
        ProcessName = $first.ProcessName
        PID         = $first.PID
        HandleCount = $_.Count
        LockedPaths = $paths
    }
}

if ($uniqueProcesses.Count -eq 0) {
    $result = Show-Message -Text "No locking handles found for:`n$Path`n`nThe folder may be locked by:`n- Explorer preview pane or an open folder window`n- A process whose working directory is here (not visible via handles)`n`nRestart Windows Explorer to release common locks?" -Buttons YesNo -Icon Question
    if ($result -eq "Yes") {
        Stop-Process -Name "explorer" -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-Process "explorer.exe"
        Show-Message -Text "Explorer has been restarted.`nTry your folder operation again." -Icon Information
    }
    exit 0
}

# Show selection grid
$selected = $uniqueProcesses | Out-GridView -Title "Select processes to terminate (Ctrl+Click for multi-select)" -PassThru

if (-not $selected -or $selected.Count -eq 0) {
    Write-Host "No processes selected. Exiting."
    exit 0
}

# Confirm termination
$procList = ($selected | ForEach-Object { "  - $($_.ProcessName) (PID $($_.PID))" }) -join "`n"
$confirm = Show-Message -Text "Terminate the following $($selected.Count) process(es)?`n`n$procList`n`nUnsaved data in these applications will be lost." -Buttons YesNo -Icon Warning

if ($confirm -ne "Yes") {
    Write-Host "Cancelled by user."
    exit 0
}

# Terminate
$killed = @()
$failed = @()
foreach ($proc in $selected) {
    try {
        Stop-Process -Id $proc.PID -Force -ErrorAction Stop
        $killed += $proc
    } catch {
        $failed += "$($proc.ProcessName) (PID $($proc.PID)): $_"
    }
}

# Restart Explorer if it was killed (it auto-restarts usually, but be safe)
if ($killed | Where-Object { $_.ProcessName -eq "explorer.exe" }) {
    Start-Sleep -Seconds 1
    if (-not (Get-Process -Name "explorer" -ErrorAction SilentlyContinue)) {
        Start-Process "explorer.exe"
    }
}

# Report
$report = @()
if ($killed.Count -gt 0) {
    $report += "Terminated $($killed.Count) process(es):"
    $killed | ForEach-Object { $report += "  - $($_.ProcessName) (PID $($_.PID))" }
}
if ($failed.Count -gt 0) {
    $report += ""
    $report += "Failed to terminate $($failed.Count) process(es):"
    $failed | ForEach-Object { $report += "  - $_" }
}
$report += ""
$report += "Try your folder operation again."

Show-Message -Text ($report -join "`n") -Icon Information
