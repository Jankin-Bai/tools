<#
.SYNOPSIS
    Git Sync: cloud-first. Align local to origin, then commit and push.

.DESCRIPTION
    Flow: preflight -> git fetch -> compare local vs origin ->
      if in sync: commit (if dirty) + push
      if out of sync: stash -> reset --hard origin/<branch> -> stash pop -> commit + push
    Proxy: uses git's default (system env / git config), nothing hardcoded.

.PARAMETER Path
    Full path of the target folder. Passed automatically as "%1" / "%V".

.PARAMETER NoPause
    Skip the final Read-Host pause. Used by CLI .cmd shims and MCP.

.PARAMETER Json
    Machine-readable JSON output. No MessageBox, no console colors, no pause.

.PARAMETER Force
    Skip confirmation. In out-of-sync state, proceeds with cloud-first reset --hard.

.NOTES
    Debug log: %TEMP%\git-sync-debug.log
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Path,
    [switch]$NoPause,
    [switch]$Json,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent

Import-Module (Join-Path $ScriptDir "..\_shared\MyTools.Common.psm1") -Force
Set-MyToolsMode -Json:$Json -DebugLogName "git-sync"

function Invoke-Git {
    param([string[]]$Arguments)
    return Invoke-Native -Command "git" -Arguments $Arguments -EchoOutput
}

function Get-GitText {
    param([string[]]$Arguments)
    $r = Invoke-Native -Command "git" -Arguments $Arguments
    if ($null -eq $r -or $r.ExitCode -ne 0) { return "" }
    if ($null -eq $r.Output) { return "" }
    $text = ($r.Output | Where-Object { $_ -is [string] }) -join "`n"
    if ($null -eq $text) { return "" }
    return $text
}

function New-CommitMessage {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    $stat = Get-GitText @("diff", "--cached", "--stat")
    $summary = ""
    if ($stat -match "(\d+)\s+files?\s+changed") {
        $files = $Matches[1]
        $adds = "0"; $dels = "0"
        if ($stat -match "(\d+)\s+insertions?") { $adds = $Matches[1] }
        if ($stat -match "(\d+)\s+deletions?") { $dels = $Matches[1] }
        $summary = " ($files files, +$adds -$dels)"
    }
    return "sync at $timestamp$summary"
}

function Invoke-CommitAndPush {
    param($result, [string]$branch)

    $statusTxt = Get-GitText @("status", "--porcelain")
    $dirty = -not [string]::IsNullOrWhiteSpace($statusTxt)

    if (-not $dirty) {
        $result.status = "nothing_to_commit"
        if (-not $Json) { Write-Host "  No changes to commit." -ForegroundColor Yellow }
        $pushR = Invoke-Git @("push", "origin", $branch)
        if ($pushR.ExitCode -eq 0) { $result.pushed = $true }
        return $result
    }

    $addR = Invoke-Git @("add", "-A")
    if ($addR.ExitCode -ne 0) {
        $result.errors += "git add failed"
        throw "git add failed"
    }

    $msg = New-CommitMessage
    $result.commit_message = $msg
    if (-not $Json) { Write-Host "  Committing: '$msg'" -ForegroundColor Gray }

    $commitR = Invoke-Git @("commit", "-m", $msg)
    if ($commitR.ExitCode -ne 0) {
        $err = ($commitR.Output | Where-Object { $_ -is [string] }) -join " "
        $result.errors += "git commit failed: $err"
        throw "git commit failed"
    }
    $line = ($commitR.Output | Where-Object { $_ -match "^\[.*\]" } | Select-Object -First 1)
    if ($line -match "\[.*\s+([a-f0-9]+)\]") { $result.commit = $Matches[1] }
    if (-not $Json -and $line) { Write-Host "  $line" -ForegroundColor Green }

    $pushR = Invoke-Git @("push", "origin", $branch)
    if ($pushR.ExitCode -ne 0) {
        $err = ($pushR.Output | Where-Object { $_ -is [string] }) -join " "
        $result.errors += "git push failed: $err"
        $result.pushed = $false
        throw "git push failed"
    }
    $result.pushed = $true
    $result.status = "ok"
    return $result
}

function Main {
    $result = New-ToolResult -Status "running" -Target $Path
    $result.branch = $null
    $result.local_sha = $null
    $result.remote_sha = $null
    $result.commit = $null
    $result.commit_message = $null
    $result.pushed = $false
    $result.aligned_to_cloud = $false
    $result.discarded_commits = @()
    $result.stash_used = $false

    Write-DebugLog "=== Git Sync started (cloud-first) ==="
    Write-DebugLog "Path: $Path | Force: $Force"

    # ---------- 1. Preflight ----------
    if (-not (Test-Path -LiteralPath $Path)) {
        $result.errors += "Path not found: $Path"
        throw "Path not found: $Path"
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item.PSIsContainer) {
        $result.errors += "Target is not a folder: $Path"
        throw "Target is not a folder"
    }

    $gitVer = Invoke-Git @("--version")
    if ($gitVer.ExitCode -ne 0) {
        $result.errors += "git.exe not found in PATH"
        throw "git not found"
    }

    Push-Location -LiteralPath $Path
    try {
        $inRepo = Invoke-Git @("rev-parse", "--is-inside-work-tree")
        if ($inRepo.ExitCode -ne 0) {
            $result.errors += "Not a git repository: $Path"
            throw "Not a git repository"
        }

        $userName = (Get-GitText @("config", "user.name")).Trim()
        $userEmail = (Get-GitText @("config", "user.email")).Trim()
        if (-not $userName -or -not $userEmail) {
            $result.errors += "git user.name/user.email not configured"
            throw "git user identity not configured. Run: git config --global user.name '<name>' ; git config --global user.email '<email>'"
        }
        Write-DebugLog "git user: $userName <$userEmail>"

        $proxy = (Get-GitText @("config", "--get", "http.proxy")).Trim()
        if ($proxy) {
            Write-DebugLog "git http.proxy: $proxy"
        } else {
            Write-DebugLog "git http.proxy: (none — using system/env default)"
        }

        $branch = (Get-GitText @("rev-parse", "--abbrev-ref", "HEAD")).Trim()
        if ($branch -eq "HEAD") {
            $result.errors += "Detached HEAD. Create a branch before syncing."
            throw "Detached HEAD. Please create a branch first."
        }
        $result.branch = $branch
        Write-DebugLog "Branch: $branch"

        $remotes = Get-GitText @("remote")
        if ($remotes -notmatch "(?m)^origin$") {
            $result.errors += "No 'origin' remote configured"
            throw "No origin remote"
        }

        # ---------- 2. Fetch ----------
        if (-not $Json) { Write-Host "`n[1/4] Fetching origin..." -ForegroundColor Cyan }
        $fetchR = Invoke-Git @("fetch", "origin")
        if ($fetchR.ExitCode -ne 0) {
            $err = ($fetchR.Output | Where-Object { $_ -is [string] }) -join " "
            $result.errors += "git fetch failed: $err"
            throw "git fetch failed (network or auth). Local repository untouched."
        }

        # ---------- 3. Compare ----------
        $localSha = (Get-GitText @("rev-parse", "HEAD")).Trim()
        $remoteSha = (Get-GitText @("rev-parse", "origin/$branch")).Trim()
        $result.local_sha = $localSha
        $result.remote_sha = $remoteSha

        $statusTxt = Get-GitText @("status", "--porcelain")
        $dirty = -not [string]::IsNullOrWhiteSpace($statusTxt)
        $inSync = ($localSha -eq $remoteSha)

        Write-DebugLog "Local: $localSha | Remote: $remoteSha | inSync: $inSync | dirty: $dirty"

        if (-not $Json) {
            Write-Host "  Branch: $branch" -ForegroundColor Gray
            Write-Host "  Local:  $localSha" -ForegroundColor Gray
            Write-Host "  Remote: $remoteSha" -ForegroundColor Gray
            $state = if ($inSync) { "IN SYNC" } else { "OUT OF SYNC" }
            $color = if ($inSync) { "Green" } else { "Yellow" }
            $extra = if ($dirty) { " + uncommitted changes" } else { "" }
            Write-Host "  State: $state$extra" -ForegroundColor $color
        }

        # ---------- 4a. In sync ----------
        if ($inSync) {
            $result.aligned_to_cloud = $true
            if (-not $dirty) {
                $result.status = "up_to_date"
                if (-not $Json) { Write-Host "`n  Already up to date. Nothing to do." -ForegroundColor Green }
                Write-DebugLog "Up to date, nothing to do."
                return $result
            }
            if (-not $Json) { Write-Host "`n[2/4] Committing and pushing..." -ForegroundColor Cyan }
            return Invoke-CommitAndPush -result $result -branch $branch
        }

        # ---------- 4b. Out of sync — cloud-first ----------
        $result.aligned_to_cloud = $false

        $discardedTxt = Get-GitText @("log", "origin/$branch..HEAD", "--oneline")
        if ($discardedTxt) {
            $result.discarded_commits = $discardedTxt -split "`n" | Where-Object { $_.Trim() }
            foreach ($c in $result.discarded_commits) { Write-DebugLog "  discard: $c" }
        }

        if (-not $Json) {
            Write-Host "`n[2/4] Cloud-first alignment" -ForegroundColor Cyan
            Write-Host "  Local is OUT OF SYNC with origin/$branch." -ForegroundColor Yellow
            if ($result.discarded_commits.Count -gt 0) {
                Write-Host "  Local commits that will be DISCARDED:" -ForegroundColor Red
                foreach ($c in $result.discarded_commits) { Write-Host "    - $c" -ForegroundColor Red }
            }
            Write-Host "  Working directory changes will be preserved via stash." -ForegroundColor Yellow
        }

        # Confirmation
        if ($Json -and -not $Force) {
            $result.status = "needs_confirmation"
            $result.errors += "Local and remote are out of sync. Pass force=true to proceed with cloud-first reset --hard."
            return $result
        }
        if (-not $Force -and -not $Json) {
            $confirm = Read-Host "`n  Proceed with cloud-first alignment? (y/N)"
            if ($confirm -notmatch "^[yY]$") {
                $result.status = "aborted_by_user"
                if (-not $Json) { Write-Host "  Aborted by user." -ForegroundColor Yellow }
                return $result
            }
        }

        # Stash
        $stashCreated = $false
        if ($dirty) {
            if (-not $Json) { Write-Host "  Stashing working directory changes..." -ForegroundColor Gray }
            $stashR = Invoke-Git @("stash", "push", "-u", "-m", "git-sync-auto-stash")
            if ($stashR.ExitCode -ne 0) {
                $result.errors += "git stash failed"
                throw "git stash failed"
            }
            $stashCreated = $true
            $result.stash_used = $true
            Write-DebugLog "Stash created."
        }

        # Reset --hard to origin
        if (-not $Json) { Write-Host "  Resetting --hard to origin/$branch..." -ForegroundColor Gray }
        $resetR = Invoke-Git @("reset", "--hard", "origin/$branch")
        if ($resetR.ExitCode -ne 0) {
            $result.errors += "git reset --hard failed"
            throw "git reset --hard failed"
        }
        $result.aligned_to_cloud = $true
        Write-DebugLog "Reset to origin/$branch successful."

        # Stash pop
        if ($stashCreated) {
            if (-not $Json) { Write-Host "  Restoring stashed changes..." -ForegroundColor Gray }
            $popR = Invoke-Git @("stash", "pop")
            if ($popR.ExitCode -ne 0) {
                $err = ($popR.Output | Where-Object { $_ -is [string] }) -join " "
                $result.errors += "git stash pop conflict: $err"
                $result.status = "stash_conflict"
                if (-not $Json) {
                    Write-Host "`n  *** STASH POP CONFLICT ***" -ForegroundColor Red
                    Write-Host "  Stash was NOT dropped. Resolve conflicts manually:" -ForegroundColor Yellow
                    Write-Host "    git add <files> ; git stash drop ; git commit ; git push" -ForegroundColor Gray
                }
                Write-DebugLog "Stash pop conflict. Stash retained for manual resolution."
                throw "Stash pop conflict. Manual resolution required."
            }
            Write-DebugLog "Stash popped successfully."
        }

        # Commit & push
        if (-not $Json) { Write-Host "`n[3/4] Committing and pushing..." -ForegroundColor Cyan }
        return Invoke-CommitAndPush -result $result -branch $branch
    } finally {
        Pop-Location
    }
}

# ============================================================
# Entry point
# ============================================================
try {
    $result = Main
    # ---------- 5. Verify (only on success) ----------
    if ($result.status -in @("ok", "nothing_to_commit") -and $result.pushed) {
        Push-Location -LiteralPath $Path
        try {
            if (-not $Json) { Write-Host "`n[4/4] Verifying..." -ForegroundColor Cyan }
            Invoke-Native -Command "git" -Arguments @("fetch", "origin") | Out-Null
            $vLocal = (Get-GitText @("rev-parse", "HEAD")).Trim()
            $vRemote = (Get-GitText @("rev-parse", "origin/$($result.branch)")).Trim()
            if ($vLocal -eq $vRemote) {
                if (-not $Json) { Write-Host "  Verified: local == origin/$($result.branch) ($vLocal)" -ForegroundColor Green }
                Write-DebugLog "Verification passed: local == remote"
            } else {
                $result.errors += "Verification failed: local=$vLocal remote=$vRemote"
                if (-not $Json) { Write-Host "  WARNING: local != remote after push!" -ForegroundColor Red }
            }
        } finally {
            Pop-Location
        }
    }
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
