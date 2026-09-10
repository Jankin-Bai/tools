<#
.SYNOPSIS
    Git Sync: add, commit, and push the current repository to origin main.

.DESCRIPTION
    Launched from "My Tools > Git Sync" right-click context menu on a folder.
    Runs: git add -A && git commit -m "sync at <timestamp>" && git push origin main
    Handles: not a git repo, no changes, push failures, detached HEAD.

.PARAMETER Path
    Full path of the right-clicked folder. Passed automatically as "%1".

.NOTES
    Debug log: %TEMP%\git-sync-debug.log
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent

Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop

# ============================================================
# Helpers
# ============================================================
function Show-Message {
    param(
        [string]$Text,
        [string]$Title = "Git Sync",
        [System.Windows.Forms.MessageBoxButtons]$Buttons = [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]$Icon = [System.Windows.Forms.MessageBoxIcon]::Information
    )
    return [System.Windows.Forms.MessageBox]::Show($Text, $Title, $Buttons, $Icon)
}

function Test-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Git {
    param([string[]]$Arguments)
    $cmdLine = "git " + ($Arguments -join " ")
    Write-DebugLog "  > $cmdLine"
    # Temporarily relax ErrorActionPreference so git stderr warnings (LF/CRLF etc.)
    # are not treated as terminating errors under $ErrorActionPreference = "Stop".
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @Arguments 2>&1 | ForEach-Object { "$_" }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEAP
    }
    if ($output) { $output | ForEach-Object { Write-DebugLog "    $_" } }
    Write-DebugLog "  exit code: $exitCode"
    return @{ Output = $output; ExitCode = $exitCode }
}

# ============================================================
# Debug logging
# ============================================================
$DebugLog = Join-Path $env:TEMP "git-sync-debug.log"
function Write-DebugLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -Path $DebugLog -Value $line -Encoding UTF8
    Write-Host $line
}

# ============================================================
# Main
# ============================================================
function Main {
    Write-DebugLog "=== Git Sync started ==="
    Write-DebugLog "Path: $Path"
    Write-DebugLog "PSVersion: $($PSVersionTable.PSVersion)"

    # --- Validate: must be a folder ---
    if (-not (Test-Path -LiteralPath $Path)) {
        Show-Message -Text "Path not found:`n$Path" -Icon Error
        throw "Path not found: $Path"
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item.PSIsContainer) {
        Show-Message -Text "Git Sync works on folders only.`n`nRight-click a folder that contains a git repository." -Icon Error
        throw "Target is not a folder: $Path"
    }

    # --- Check git is installed ---
    try {
        $gitVer = & git --version 2>&1
        Write-DebugLog "Git: $gitVer"
    } catch {
        Show-Message -Text "git.exe not found in PATH.`n`nInstall Git for Windows and try again." -Icon Error
        throw "git not found"
    }

    # --- Change to target directory ---
    Write-DebugLog "Changing to: $Path"
    Set-Location -LiteralPath $Path

    # --- Check this is a git repo ---
    $revParse = Invoke-Git @("rev-parse", "--is-inside-work-tree")
    if ($revParse.ExitCode -ne 0) {
        Show-Message -Text "Not a git repository:`n$Path`n`nRun 'git init' first." -Icon Error
        throw "Not a git repository"
    }

    # --- Check current branch ---
    $branchResult = Invoke-Git @("rev-parse", "--abbrev-ref", "HEAD")
    $branch = ($branchResult.Output | Where-Object { $_ -is [string] } | Select-Object -First 1).Trim()
    Write-DebugLog "Current branch: $branch"

    # --- Check for remote ---
    $remoteResult = Invoke-Git @("remote")
    $hasOrigin = ($remoteResult.Output | Where-Object { $_ -match "^origin$" }).Count -gt 0
    if (-not $hasOrigin) {
        Show-Message -Text "No 'origin' remote configured.`n`nRun: git remote add origin <url>" -Icon Error
        throw "No origin remote"
    }

    # --- git add -A ---
    Write-Host "`n[1/3] Staging changes..." -ForegroundColor Cyan
    $addResult = Invoke-Git @("add", "-A")
    if ($addResult.ExitCode -ne 0) {
        Show-Message -Text "git add failed:`n$($addResult.Output -join "`n")" -Icon Error
        throw "git add failed"
    }

    # --- Check if there's anything to commit ---
    $statusResult = Invoke-Git @("status", "--porcelain")
    $hasChanges = $false
    if ($statusResult.Output) {
        $hasChanges = ($statusResult.Output | Where-Object { $_ -is [string] -and $_.Trim().Length -gt 0 }).Count -gt 0
    }

    if (-not $hasChanges) {
        Write-DebugLog "No changes to commit"
        Write-Host "  No changes to commit." -ForegroundColor Yellow
        # Still try push in case local is ahead
        Write-Host "`n[3/3] Pushing..." -ForegroundColor Cyan
        $pushResult = Invoke-Git @("push", "origin", $branch)
        if ($pushResult.ExitCode -eq 0) {
            Show-Message -Text "Nothing to commit.`nPush to origin/$branch completed." -Icon Information
        } else {
            Show-Message -Text "Nothing to commit, but push failed:`n$($pushResult.Output -join "`n")" -Icon Warning
        }
        return
    }

    # --- git commit ---
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    $commitMsg = "sync at $timestamp"
    Write-Host "`n[2/3] Committing: '$commitMsg'" -ForegroundColor Cyan
    $commitResult = Invoke-Git @("commit", "-m", $commitMsg)
    if ($commitResult.ExitCode -ne 0) {
        Show-Message -Text "git commit failed:`n$($commitResult.Output -join "`n")" -Icon Error
        throw "git commit failed"
    }
    $commitLine = ($commitResult.Output | Where-Object { $_ -match "^\[.*\]" } | Select-Object -First 1)
    if ($commitLine) { Write-Host "  $commitLine" -ForegroundColor Green }

    # --- git push origin main ---
    Write-Host "`n[3/3] Pushing to origin/$branch..." -ForegroundColor Cyan
    $pushResult = Invoke-Git @("push", "origin", $branch)
    if ($pushResult.ExitCode -ne 0) {
        $pushErr = $pushResult.Output -join "`n"
        Write-DebugLog "Push failed: $pushErr"
        Show-Message -Text "git push failed:`n$pushErr`n`nCommon causes:`n- Network not connected`n- Remote requires authentication`n- Local branch is behind (pull first)" -Icon Error
        throw "git push failed"
    }

    Write-Host "`n=== Sync complete ===" -ForegroundColor Green
    Write-DebugLog "Sync completed successfully"
    Show-Message -Text "Git sync completed successfully.`n`nBranch: $branch`nCommit: $commitMsg`nRemote: origin/$branch" -Icon Information
}

# ============================================================
# Entry point
# ============================================================
try {
    Main
} catch {
    $errMsg = $_.Exception.Message
    Write-DebugLog "FATAL ERROR: $errMsg"
    Write-DebugLog "Stack: $($_.ScriptStackTrace)"
    Write-Host "`n=== ERROR ===" -ForegroundColor Red
    Write-Host $errMsg -ForegroundColor Red
    try { Show-Message -Text "Error:`n$errMsg`n`nDebug log:`n$DebugLog" -Icon Error } catch { }
} finally {
    Write-Host "`nDebug log: $DebugLog" -ForegroundColor Yellow
    Read-Host "Press Enter to close"
}
