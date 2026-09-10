<#
.SYNOPSIS
    Unlock a file or folder by closing locking processes, with fallback to
    delete-on-reboot.

.DESCRIPTION
    Pipeline architecture: Detect -> Terminate -> Verify.
    Detection layers: Restart Manager API, Sysinternals handle.exe, module scan.

.PARAMETER Path
    Full path of the file or folder to unlock.

.PARAMETER NoPause
    Skip the final Read-Host pause. Used by CLI .cmd shims and MCP.

.PARAMETER Json
    Machine-readable JSON output. Without -Force: detection only.

.PARAMETER Force
    Auto-confirm all destructive actions (use with -Json for MCP automation).

.NOTES
    Debug log: %TEMP%\unlock-debug.log
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
$BinDir    = (Resolve-Path (Join-Path $ScriptDir "..\..\bin")).Path

Import-Module (Join-Path $ScriptDir "..\_shared\MyTools.Common.psm1") -Force
Set-MyToolsMode -Json:$Json -DebugLogName "unlock"

# ============================================================
# P/Invoke: Restart Manager + MoveFileEx
# ============================================================
$rmCode = @"
using System;
using System.Runtime.InteropServices;

public static class RestartManager {
    [StructLayout(LayoutKind.Sequential)]
    public struct RM_UNIQUE_PROCESS {
        public int dwProcessId;
        public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
    }
    public enum RM_APP_TYPE {
        RmUnknownApp=0, RmMainWindow=1, RmOtherWindow=2,
        RmService=3, RmExplorer=4, RmConsole=5, RmCritical=1000
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct RM_PROCESS_INFO {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=64)] public string strServiceShortName;
        public RM_APP_TYPE ApplicationType;
        public uint AppStatus; public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
    }
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmStartSession(out uint h, int f, string k);
    [DllImport("rstrtmgr.dll")] public static extern int RmEndSession(uint h);
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmRegisterResources(uint h, uint nf, string[] files, uint na, RM_UNIQUE_PROCESS[] apps, uint ns, string[] svcs);
    [DllImport("rstrtmgr.dll")] public static extern int RmGetList(uint h, out uint need, ref uint cnt, [In,Out] RM_PROCESS_INFO[] apps, ref uint reboot);
    [DllImport("rstrtmgr.dll")] public static extern int RmShutdown(uint h, int flags, IntPtr status);
}

public static class Kernel32 {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool MoveFileEx(string lpExistingFileName, string lpNewFileName, int dwFlags);
    public const int MOVEFILE_DELAY_UNTIL_REBOOT = 0x4;
}
"@
try { Add-Type -TypeDefinition $rmCode -ErrorAction Stop } catch { throw "P/Invoke compile failed: $_" }

# ============================================================
# Pipeline Phase 1+2: Detection
# ============================================================
function Invoke-Detection {
    param([string]$TargetPath, [bool]$IsFolder)

    $allLocks = @()

    # --- Phase 1: Restart Manager ---
    if (-not $Json) { Write-Host "`nPhase 1: Restart Manager..." -ForegroundColor Cyan }
    Write-DebugLog "Phase 1 started"

    $resources = New-Object System.Collections.Generic.List[string]
    $resources.Add($TargetPath)
    if ($IsFolder) {
        $fileCount = 0
        try {
            $allFiles = [System.IO.Directory]::EnumerateFiles($TargetPath, "*", [System.IO.SearchOption]::AllDirectories)
            foreach ($f in $allFiles) {
                if ($fileCount -ge 5000) { break }
                $resources.Add($f); $fileCount++
            }
        } catch { }
        Write-DebugLog "  Registered $($resources.Count) resources"
    }

    $sessionHandle = 0
    $rmSurvivors = @()
    $hr = [RestartManager]::RmStartSession([ref]$sessionHandle, 0, [Guid]::NewGuid().ToString())
    if ($hr -eq 0) {
        [RestartManager]::RmRegisterResources($sessionHandle, $resources.Count, $resources.ToArray(), 0, $null, 0, $null) | Out-Null
        $needed = 0; $count = 0; $reboot = 0
        [RestartManager]::RmGetList($sessionHandle, [ref]$needed, [ref]$count, $null, [ref]$reboot) | Out-Null
        $rmProcesses = @()
        if ($needed -gt 0) {
            $procInfo = New-Object RestartManager+RM_PROCESS_INFO[] $needed
            $count = $needed
            [RestartManager]::RmGetList($sessionHandle, [ref]$needed, [ref]$count, $procInfo, [ref]$reboot) | Out-Null
            foreach ($p in $procInfo) {
                if ($p.Process.dwProcessId -gt 0) {
                    $rmProcesses += [PSCustomObject]@{
                        PID = $p.Process.dwProcessId; Name = $p.strAppName
                        Critical = ($p.ApplicationType -eq [RestartManager+RM_APP_TYPE]::RmCritical)
                    }
                }
            }
        }
        Write-DebugLog "  Restart Manager found $($rmProcesses.Count) process(es)"

        if ($rmProcesses.Count -gt 0) {
            $closable = $rmProcesses | Where-Object { -not $_.Critical }
            $procList = ($closable | ForEach-Object { "  - $($_.Name) (PID $($_.PID))" }) -join "`n"
            if ((Show-Message -Text "Restart Manager found $($closable.Count) process(es):`n$procList`n`nAttempt graceful shutdown?" -Title "Unlock" -Buttons YesNo -Icon Question) -eq "Yes") {
                $hr = [RestartManager]::RmShutdown($sessionHandle, 0, [IntPtr]::Zero)
                Write-DebugLog "  RmShutdown returned 0x$('{0:X}' -f $hr)"
                Start-Sleep -Seconds 2
                foreach ($rp in $closable) {
                    if (Get-Process -Id $rp.PID -ErrorAction SilentlyContinue) {
                        $rmSurvivors += [PSCustomObject]@{ ProcessName=$rp.Name; PID=$rp.PID; HandleType="RMSurvivor"; TargetPath=$TargetPath }
                    }
                }
            } else {
                foreach ($rp in $closable) {
                    $rmSurvivors += [PSCustomObject]@{ ProcessName=$rp.Name; PID=$rp.PID; HandleType="RMSurvivor"; TargetPath=$TargetPath }
                }
            }
        }
        [RestartManager]::RmEndSession($sessionHandle) | Out-Null
    }

    # --- Phase 2: handle.exe + module scan ---
    if (-not $Json) { Write-Host "`nPhase 2: Detecting remaining locks..." -ForegroundColor Cyan }
    Write-DebugLog "Phase 2 started"

    $handleExe = Get-HandleExePath
    if (-not $handleExe) { throw "handle.exe not found in: $BinDir" }

    $allLocks += Invoke-HandleScan -TargetPath $TargetPath -HandleExe $handleExe
    $allLocks += Get-LockingModules -TargetPath $TargetPath -IsFolder $IsFolder
    $allLocks += $rmSurvivors

    $lockPids = $allLocks | Select-Object -ExpandProperty PID -Unique
    Write-DebugLog "  Total unique locking PIDs: $($lockPids.Count)"
    $allLocks | ForEach-Object { Write-DebugLog "    Lock: $($_.ProcessName) PID=$($_.PID) [$($_.HandleType)]" }

    return @{ Locks = $allLocks; LockPids = $lockPids; HandleExe = $handleExe }
}

# ============================================================
# Pipeline Phase 3: Termination
# ============================================================
function Invoke-Termination {
    param([object[]]$LockPids)

    $killed = @(); $failed = @()
    $targets = @()

    foreach ($pidVal in $LockPids) {
        $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
        if ($proc) {
            $isCritical = $proc.ProcessName -match "^(csrss|smss|wininit|winlogon|services|lsass|System|Registry|fontdrvhost)$"
            if (-not $isCritical) {
                $targets += [PSCustomObject]@{ ProcessName = $proc.ProcessName; PID = $pidVal }
            } else {
                Write-DebugLog "  Skipping critical: $($proc.ProcessName) PID=$pidVal"
            }
        }
    }

    if ($targets.Count -eq 0) { return @{ Killed = @(); Failed = @() } }

    if (-not $Json) { Write-Host "`nPhase 3: Terminating locks..." -ForegroundColor Cyan }
    $list = ($targets | ForEach-Object { "  - $($_.ProcessName).exe (PID $($_.PID))" }) -join "`n"
    if ((Show-Message -Text "Terminate $($targets.Count) process(es)?`n`n$list`n`nUnsaved data will be lost." -Title "Unlock" -Buttons YesNo -Icon Warning) -ne "Yes") {
        Write-DebugLog "  User cancelled termination"
        return @{ Killed = @(); Failed = @(); Cancelled = $true }
    }

    foreach ($t in $targets) {
        try {
            Stop-Process -Id $t.PID -Force -ErrorAction Stop
            $killed += $t
        } catch {
            $failed += "$($t.ProcessName) (PID $($t.PID)): $_"
        }
    }
    Write-DebugLog "  Killed: $($killed.Count), Failed: $($failed.Count)"

    if ($killed | Where-Object { $_.ProcessName -eq "explorer" }) {
        Start-Sleep 1
        if (-not (Get-Process -Name "explorer" -ErrorAction SilentlyContinue)) { Start-Process "explorer.exe" }
    }
    Start-Sleep -Seconds 1

    return @{ Killed = $killed; Failed = $failed; Cancelled = $false }
}

# ============================================================
# Pipeline Phase 4: Verification
# ============================================================
function Invoke-Verification {
    param([string]$TargetPath, [bool]$IsFolder, [string]$HandleExe)

    if (-not $Json) { Write-Host "`nPhase 4: Verifying..." -ForegroundColor Cyan }
    Write-DebugLog "Phase 4: re-scanning"

    $verifyLocks = Invoke-HandleScan -TargetPath $TargetPath -HandleExe $HandleExe
    $verifyModules = Get-LockingModules -TargetPath $TargetPath -IsFolder $IsFolder
    $remaining = @($verifyLocks) + @($verifyModules)
    $remainingPids = $remaining | Select-Object -ExpandProperty PID -Unique
    Write-DebugLog "  Remaining after kill: $($remainingPids.Count) PID(s)"

    return @{ Remaining = $remaining; RemainingPids = $remainingPids }
}

# ============================================================
# Detection helpers
# ============================================================
function Get-HandleExePath {
    $exeName = if ([Environment]::Is64BitOperatingSystem) { "handle64.exe" } else { "handle.exe" }
    $exePath = Join-Path $BinDir $exeName
    if (-not (Test-Path $exePath)) { $exePath = Join-Path $BinDir "handle.exe" }
    if (-not (Test-Path $exePath)) { return $null }
    $eulaKey = "HKCU:\Software\Sysinternals\Handle"
    if (-not (Test-Path $eulaKey)) { New-Item -Path $eulaKey -Force | Out-Null }
    Set-ItemProperty -Path $eulaKey -Name "EulaAccepted" -Value 1 -Type DWord
    return $exePath
}

function Invoke-HandleScan {
    param([string]$TargetPath, [string]$HandleExe)
    $output = & $HandleExe -accepteula -nobanner $TargetPath 2>&1
    $results = @()
    foreach ($line in $output) {
        if ($line -match '^(\S+\.exe)\s+pid:\s+(\d+)\s+type:\s+(\S+)\s+([0-9A-Fa-f]+):\s+(.+)$') {
            $results += [PSCustomObject]@{
                ProcessName = $Matches[1]; PID = [int]$Matches[2]
                HandleType = $Matches[3]; TargetPath = $Matches[5].Trim()
            }
        }
    }
    Write-DebugLog "  handle.exe: $($results.Count) handle(s)"
    return $results
}

function Get-LockingModules {
    param([string]$TargetPath, [bool]$IsFolder)
    Write-DebugLog "  Scanning loaded modules (tasklist /m) ..."
    $results = @(); $dllsToCheck = @()
    if ($IsFolder) {
        try {
            $dlls = [System.IO.Directory]::EnumerateFiles($TargetPath, "*.dll", [System.IO.SearchOption]::AllDirectories) | Select-Object -First 200
            foreach ($dll in $dlls) { $dllsToCheck += [System.IO.Path]::GetFileName($dll) }
        } catch { }
    } else {
        $ext = [System.IO.Path]::GetExtension($TargetPath).ToLower()
        if ($ext -eq ".dll" -or $ext -eq ".exe") { $dllsToCheck += [System.IO.Path]::GetFileName($TargetPath) }
    }
    foreach ($dllName in $dllsToCheck) {
        try {
            $output = & tasklist /m $dllName /fo csv /nh 2>$null
            foreach ($line in $output) {
                if ($line -match '^"([^"]+)",\s*(\d+),') {
                    $results += [PSCustomObject]@{
                        ProcessName = $Matches[1] -replace '\.exe$',''; PID = [int]$Matches[2]
                        HandleType = "LoadedModule"; TargetPath = $dllName
                    }
                }
            }
        } catch { }
    }
    Write-DebugLog "  tasklist /m checked $($dllsToCheck.Count) DLL(s), found $($results.Count) module lock(s)"
    return $results
}

# ============================================================
# Main: orchestrate the pipeline
# ============================================================
function Main {
    $result = New-ToolResult -Status "running" -Target $Path
    $result["target_type"] = $null
    $result["locked_by"] = @()
    $result["terminated"] = @()
    $result["failed"] = @()

    Write-DebugLog "=== Unlock started ==="
    Write-DebugLog "Path: $Path"
    Write-DebugLog "PSVersion: $($PSVersionTable.PSVersion)"
    Write-DebugLog "Mode: $(if ($Json) {'JSON'} elseif ($NoPause) {'CLI'} else {'Interactive'}) Force=$Force"

    # --- Validate ---
    if (-not (Test-Path $Path)) {
        $result.errors += "Path not found: $Path"
        Show-Message -Text "Path not found:`n$Path" -Title "Unlock" -Icon Error
        throw "Path not found: $Path"
    }
    $item = Get-Item $Path -ErrorAction SilentlyContinue
    $isFolder = $item.PSIsContainer
    $result.target_type = if ($isFolder) { "folder" } else { "file" }
    Write-DebugLog "Target type: $(if ($isFolder) {'Folder'} else {'File'})"

    if (-not (Test-Admin)) {
        Show-Message -Text "Not running as Administrator.`nSome locks may not be visible." -Title "Unlock" -Icon Warning
        Write-DebugLog "WARNING: Not admin"
    }

    # --- Pipeline: Detect ---
    $detect = Invoke-Detection -TargetPath $Path -IsFolder $isFolder
    foreach ($lock in $detect.Locks) {
        $result.locked_by += [ordered]@{ pid=$lock.PID; name=$lock.ProcessName; type=$lock.HandleType; target=$lock.TargetPath }
    }

    if ($detect.LockPids.Count -eq 0) {
        if ($Json) {
            $result.status = "no_locks_detected"
            return $result
        }
        $choice = Show-Message -Text "No locking processes detected.`n`nRestart Explorer? Or mark for delete-on-reboot?" -Title "Unlock" -Buttons YesNoCancel -Icon Question
        if ($choice -eq "Yes") {
            Stop-Process -Name "explorer" -Force -ErrorAction SilentlyContinue
            Start-Sleep 2; Start-Process "explorer.exe"
        } elseif ($choice -eq "Cancel") {
            $result.status = "no_locks_detected"
            return $result
        }
        # "No" falls through to delete-on-reboot
    }

    # --- Pipeline: Terminate (skip if Json without Force and locks exist) ---
    if ($detect.LockPids.Count -gt 0) {
        if ($Json -and -not $Force) {
            Write-DebugLog "  Json mode without Force: detection only, returning needs_confirmation"
            $result.status = "needs_confirmation"
            return $result
        }
        $term = Invoke-Termination -LockPids $detect.LockPids
        if ($term.Cancelled) {
            $result.status = "needs_confirmation"
            return $result
        }
        foreach ($k in $term.Killed) {
            $result.terminated += [ordered]@{ pid=$k.PID; name=$k.ProcessName; method="force_kill" }
        }
        foreach ($f in $term.Failed) { $result.failed += $f }
    }

    # --- Pipeline: Verify ---
    $verify = Invoke-Verification -TargetPath $Path -IsFolder $isFolder -HandleExe $detect.HandleExe

    if ($verify.RemainingPids.Count -eq 0) {
        $result.status = "ok"
        Show-Message -Text "All locks released successfully." -Title "Unlock"
        return $result
    }

    # --- Locks remain: offer delete-on-reboot ---
    $remList = ($verify.Remaining | Group-Object PID | ForEach-Object { "  - $($_.Group[0].ProcessName) (PID $($_.Name))" }) -join "`n"
    if ((Show-Message -Text "Locks still held by:`n$remList`n`nMark for deletion on next reboot?" -Title "Unlock" -Buttons YesNo -Icon Warning) -eq "Yes") {
        $ok = [Kernel32]::MoveFileEx($Path, $null, [Kernel32]::MOVEFILE_DELAY_UNTIL_REBOOT)
        if ($ok) {
            Write-DebugLog "  Marked for delete-on-reboot: $Path"
            $result.status = "marked_delete_on_reboot"
            Show-Message -Text "Successfully marked for deletion on next reboot." -Title "Unlock"
        } else {
            $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            $result.errors += "MoveFileEx failed, error=$err"
            $result.status = "partial"
            Show-Message -Text "Failed to mark for delete-on-reboot (error $err)." -Title "Unlock" -Icon Error
        }
    } else {
        $result.status = "partial"
    }

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
    Write-DebugLog "Stack: $($_.ScriptStackTrace)"
    if (-not $result) { $result = New-ToolResult -Status "error" -Target $Path }
    $result.status = "error"
    if ($result.errors -notcontains $errMsg) { $result.errors += $errMsg }
    if (-not $Json) {
        Write-Host "`n=== ERROR ===" -ForegroundColor Red
        Write-Host $errMsg -ForegroundColor Red
        try { Show-Message -Text "Error:`n$errMsg`n`nDebug log:`n$(Get-DebugLogPath)" -Title "Unlock" -Icon Error } catch { }
    }
} finally {
    if ($Json) {
        $result | ConvertTo-Json -Depth 10
    } else {
        Write-Host "`nDebug log: $(Get-DebugLogPath)" -ForegroundColor Yellow
        if (-not $NoPause) { Read-Host "Press Enter to close" }
    }
}
