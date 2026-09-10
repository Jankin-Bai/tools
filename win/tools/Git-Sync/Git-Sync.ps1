<#
.SYNOPSIS
    Git Sync: add, commit, and push the current repository to origin.

.DESCRIPTION
    Launched from "My Tools > Git Sync" right-click context menu on a folder.
    Runs: git add -A && git commit -m "sync at <timestamp>" && git push origin <branch>
    Handles: not a git repo, no changes, push failures, detached HEAD.

.PARAMETER Path
    Full path of the right-clicked folder. Passed automatically as "%1".

.PARAMETER NoPause
    Skip the final Read-Host pause. Used by CLI .cmd shims and MCP.

.PARAMETER Json
    Machine-readable JSON output. No MessageBox, no console colors, no pause.

.NOTES
    Debug log: %TEMP%\git-sync-debug.log
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Path,
    [switch]$NoPause,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent

Import-Module (Join-Path $ScriptDir "..\_shared\MyTools.Common.psm1") -Force
Set-MyToolsMode -Json:$Json -DebugLogName "git-sync"

function Invoke-Git {
    param([string[]]$Arguments)
    return Invoke-Native -Command "git" -Arguments $Arguments -EchoOutput
}

function Main {
    $result = New-ToolResult -Status "running" -Target $Path
    $result["branch"] = $null
    $result["commit"] = $null
    $result["commit_message"] = $null
    $result["pushed"] = $false

    Write-DebugLog "=== Git Sync started ==="
    Write-DebugLog "Path: $Path"
    Write-DebugLog "PSVersion: $($PSVersionTable.PSVersion)"
    Write-DebugLog "Mode: $(if ($Json) {'JSON'} elseif ($NoPause) {'CLI'} else {'Interactive'})"

    # --- Validate ---
    if (-not (Test-Path -LiteralPath $Path)) {
        $result.errors += "Path not found: $Path"
        Show-Message -Text "Path not found:`n$Path" -Title "Git Sync" -Icon Error
        throw "Path not found: $Path"
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item.PSIsContainer) {
        $result.errors += "Target is not a folder: $Path"
        Show-Message -Text "Git Sync works on folders only." -Title "Git Sync" -Icon Error
        throw "Target is not a folder: $Path"
    }

    # --- Check git (uses Invoke-Native, not bare & git) ---
    $gitCheck = Invoke-Git @("--version")
    if ($gitCheck.ExitCode -ne 0) {
        $result.errors += "git.exe not found in PATH"
        Show-Message -Text "git.exe not found in PATH." -Title "Git Sync" -Icon Error
        throw "git not found"
    }

    # --- Change to target directory ---
    Write-DebugLog "Changing to: $Path"
    Set-Location -LiteralPath $Path

    # --- Check git repo ---
    $revParse = Invoke-Git @("rev-parse", "--is-inside-work-tree")
    if ($revParse.ExitCode -ne 0) {
        $result.errors += "Not a git repository: $Path"
        Show-Message -Text "Not a git repository:`n$Path" -Title "Git Sync" -Icon Error
        throw "Not a git repository"
    }

    # --- Current branch ---
    $branchResult = Invoke-Git @("rev-parse", "--abbrev-ref", "HEAD")
    $branch = ($branchResult.Output | Where-Object { $_ -is [string] } | Select-Object -First 1).Trim()
    $result.branch = $branch
    Write-DebugLog "Current branch: $branch"

    # --- Check origin remote ---
    $remoteResult = Invoke-Git @("remote")
    $hasOrigin = ($remoteResult.Output | Where-Object { $_ -match "^origin$" }).Count -gt 0
    if (-not $hasOrigin) {
        $result.errors += "No 'origin' remote configured"
        Show-Message -Text "No 'origin' remote configured." -Title "Git Sync" -Icon Error
        throw "No origin remote"
    }

    # --- git add -A ---
    if (-not $Json) { Write-Host "`n[1/3] Staging changes..." -ForegroundColor Cyan }
    $addResult = Invoke-Git @("add", "-A")
    if ($addResult.ExitCode -ne 0) {
        $result.errors += "git add failed: $($addResult.Output -join ' ')"
        Show-Message -Text "git add failed." -Title "Git Sync" -Icon Error
        throw "git add failed"
    }

    # --- Check for changes ---
    $statusResult = Invoke-Git @("status", "--porcelain")
    $hasChanges = ($statusResult.Output | Where-Object { $_ -is [string] -and $_.Trim().Length -gt 0 }).Count -gt 0

    if (-not $hasChanges) {
        Write-DebugLog "No changes to commit"
        if (-not $Json) { Write-Host "  No changes to commit." -ForegroundColor Yellow }
        if (-not $Json) { Write-Host "`n[3/3] Pushing..." -ForegroundColor Cyan }
        $pushResult = Invoke-Git @("push", "origin", $branch)
        if ($pushResult.ExitCode -eq 0) {
            $result.status = "nothing_to_commit"
            $result.pushed = $true
            Show-Message -Text "Nothing to commit.`nPush to origin/$branch completed." -Title "Git Sync"
        } else {
            $result.status = "nothing_to_commit"
            $result.pushed = $false
            $result.errors += "push failed: $($pushResult.Output -join ' ')"
            Show-Message -Text "Nothing to commit, but push failed." -Title "Git Sync" -Icon Warning
        }
        return $result
    }

    # --- git commit ---
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    $commitMsg = "sync at $timestamp"
    $result.commit_message = $commitMsg
    if (-not $Json) { Write-Host "`n[2/3] Committing: '$commitMsg'" -ForegroundColor Cyan }
    $commitResult = Invoke-Git @("commit", "-m", $commitMsg)
    if ($commitResult.ExitCode -ne 0) {
        $result.errors += "git commit failed: $($commitResult.Output -join ' ')"
        Show-Message -Text "git commit failed." -Title "Git Sync" -Icon Error
        throw "git commit failed"
    }
    $commitLine = ($commitResult.Output | Where-Object { $_ -match "^\[.*\]" } | Select-Object -First 1)
    if ($commitLine -and -not $Json) { Write-Host "  $commitLine" -ForegroundColor Green }
    if ($commitLine -match "\[.*\s+([a-f0-9]+)\]") { $result.commit = $Matches[1] }

    # --- git push ---
    if (-not $Json) { Write-Host "`n[3/3] Pushing to origin/$branch..." -ForegroundColor Cyan }
    $pushResult = Invoke-Git @("push", "origin", $branch)
    if ($pushResult.ExitCode -ne 0) {
        $pushErr = $pushResult.Output -join "`n"
        Write-DebugLog "Push failed: $pushErr"
        $result.errors += "git push failed: $pushErr"
        $result.pushed = $false
        Show-Message -Text "git push failed." -Title "Git Sync" -Icon Error
        throw "git push failed"
    }

    $result.status = "ok"
    $result.pushed = $true
    if (-not $Json) { Write-Host "`n=== Sync complete ===" -ForegroundColor Green }
    Write-DebugLog "Sync completed successfully"
    Show-Message -Text "Git sync completed successfully.`n`nBranch: $branch`nCommit: $commitMsg" -Title "Git Sync"
    return $result
}

# ============================================================
# Entry point
# ============================================================
try {
    $result = Main
} catch {
    $errMsg = $_.Exception.Message
    Write-DebugLog "FATAL ERROR: $errMsg"
    if (-not $result) { $result = New-ToolResult -Status "error" -Target $Path }
    $result.status = "error"
    if ($result.errors -notcontains $errMsg) { $result.errors += $errMsg }
    if (-not $Json) {
        Write-Host "`n=== ERROR ===" -ForegroundColor Red
        Write-Host $errMsg -ForegroundColor Red
        try { Show-Message -Text "Error:`n$errMsg`n`nDebug log:`n$(Get-DebugLogPath)" -Title "Git Sync" -Icon Error } catch { }
    }
} finally {
    if ($Json) {
        $result | ConvertTo-Json -Depth 10
    } else {
        Write-Host "`nDebug log: $(Get-DebugLogPath)" -ForegroundColor Yellow
        if (-not $NoPause) { Read-Host "Press Enter to close" }
    }
}
