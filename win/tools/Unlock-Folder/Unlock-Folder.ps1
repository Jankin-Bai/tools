<#
.SYNOPSIS
    Unlock a file or folder by closing locking processes, with fallback to
    delete-on-reboot.

.DESCRIPTION
    Works on both files and folders. Detection layers:
      1. Restart Manager API (graceful shutdown)
      2. Sysinternals handle.exe (file/directory handles)
      3. Process module scan (loaded DLLs from the target path)
    After killing processes, re-scans to verify locks are released.
    If locks persist, offers to mark the file/folder for deletion on next reboot.

    Launched from "My Tools > Unlock" right-click context menu (files and folders).

.PARAMETER Path
    Full path of the file or folder to unlock. Passed automatically as "%1".

.NOTES
    handle.exe is downloaded automatically to ..\..\bin\ if missing.
    Debug log: %TEMP%\unlock-debug.log
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

# ============================================================
# P/Invoke: Restart Manager + MoveFileEx (delete on reboot)
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

try {
    Add-Type -TypeDefinition $rmCode -ErrorAction Stop
} catch {
    Write-Host "FATAL: Failed to compile P/Invoke types: $_" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop

# ============================================================
# Helpers
# ============================================================
function Show-Message {
    param(
        [string]$Text,
        [string]$Title = "Unlock",
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
                ProcessName = $Matches[1]
                PID         = [int]$Matches[2]
                HandleType  = $Matches[3]
                TargetPath  = $Matches[5].Trim()
            }
        }
    }
    return $results
}

function Get-LockingModules {
    param([string]$TargetPath, [bool]$IsFolder)
    Write-DebugLog "  Scanning loaded modules (tasklist /m) ..."
    $results = @()
    $dllsToCheck = @()

    if ($IsFolder) {
        # Enumerate DLLs in folder (top 200) and check each
        try {
            $dlls = [System.IO.Directory]::EnumerateFiles($TargetPath, "*.dll", [System.IO.SearchOption]::AllDirectories) | Select-Object -First 200
            foreach ($dll in $dlls) { $dllsToCheck += [System.IO.Path]::GetFileName($dll) }
        } catch { }
    } else {
        # Single file: if it's a DLL, check by name
        $ext = [System.IO.Path]::GetExtension($TargetPath).ToLower()
        if ($ext -eq ".dll" -or $ext -eq ".exe") {
            $dllsToCheck += [System.IO.Path]::GetFileName($TargetPath)
        }
    }

    foreach ($dllName in $dllsToCheck) {
        try {
            $output = & tasklist /m $dllName /fo csv /nh 2>$null
            foreach ($line in $output) {
                if ($line -match '^"([^"]+)",\s*(\d+),') {
                    $results += [PSCustomObject]@{
                        ProcessName = $Matches[1] -replace '\.exe$',''
                        PID         = [int]$Matches[2]
                        HandleType  = "LoadedModule"
                        TargetPath  = $dllName
                    }
                }
            }
        } catch { }
    }
    Write-DebugLog "  tasklist /m checked $($dllsToCheck.Count) DLL(s), found $($results.Count) module lock(s)"
    return $results
}

# ============================================================
# Debug logging
# ============================================================
$DebugLog = Join-Path $env:TEMP "unlock-debug.log"
function Write-DebugLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -Path $DebugLog -Value $line -Encoding UTF8
    Write-Host $line
}
Write-DebugLog "=== Unlock started ==="
Write-DebugLog "Path: $Path"
Write-DebugLog "PSVersion: $($PSVersionTable.PSVersion)"

# ============================================================
# Main
# ============================================================
function Main {

# --- Validate ---
if (-not (Test-Path $Path)) {
    Show-Message -Text "Path not found:`n$Path" -Icon Error
    throw "Path not found: $Path"
}
$item = Get-Item $Path -ErrorAction SilentlyContinue
$isFolder = $item.PSIsContainer
Write-DebugLog "Target type: $(if ($isFolder) {'Folder'} else {'File'})"

if (-not (Test-Admin)) {
    Show-Message -Text "Not running as Administrator.`nSome locks may not be visible.`n`nFor full results, re-run elevated." -Icon Warning
    Write-DebugLog "WARNING: Not admin"
}

# --- Build resource list for Restart Manager ---
$resources = New-Object System.Collections.Generic.List[string]
$resources.Add($Path)
if ($isFolder) {
    $fileLimit = 5000
    $fileCount = 0
    Write-Host "  Enumerating files recursively..." -ForegroundColor DarkGray
    try {
        $allFiles = [System.IO.Directory]::EnumerateFiles($Path, "*", [System.IO.SearchOption]::AllDirectories)
        foreach ($f in $allFiles) {
            if ($fileCount -ge $fileLimit) { Write-Warning "  File limit reached."; break }
            $resources.Add($f); $fileCount++
        }
    } catch { }
    Write-DebugLog "  Registered $($resources.Count) resources (folder + files)"
}

# ============================================================
# Phase 1: Restart Manager graceful shutdown
# ============================================================
Write-Host "`nPhase 1: Restart Manager..." -ForegroundColor Cyan
Write-DebugLog "Phase 1 started"

$sessionHandle = 0
$rmSurvivors = @()
$hr = [RestartManager]::RmStartSession([ref]$sessionHandle, 0, [Guid]::NewGuid().ToString())
if ($hr -eq 0) {
    $hr = [RestartManager]::RmRegisterResources($sessionHandle, $resources.Count, $resources.ToArray(), 0, $null, 0, $null)
    if ($hr -eq 0) {
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
        $rmProcesses | ForEach-Object { Write-DebugLog "    RM: $($_.Name) PID=$($_.PID) Critical=$($_.Critical)" }

        if ($rmProcesses.Count -gt 0) {
            $closable = $rmProcesses | Where-Object { -not $_.Critical }
            $critical = $rmProcesses | Where-Object { $_.Critical }
            $procList = ($closable | ForEach-Object { "  - $($_.Name) (PID $($_.PID))" }) -join "`n"
            $msg = "Restart Manager found $($closable.Count) process(es):`n$procList"
            if ($critical) { $msg += "`n`nSkipping $($critical.Count) critical processes." }
            $msg += "`n`nAttempt graceful shutdown?"
            if ((Show-Message -Text $msg -Buttons YesNo -Icon Question) -eq "Yes") {
                $hr = [RestartManager]::RmShutdown($sessionHandle, 0, [IntPtr]::Zero)
                Write-DebugLog "  RmShutdown returned 0x$('{0:X}' -f $hr)"
                Start-Sleep -Seconds 2
                # Check which RM processes are still alive (survivors need force-kill)
                foreach ($rp in $closable) {
                    $stillAlive = Get-Process -Id $rp.PID -ErrorAction SilentlyContinue
                    if ($stillAlive) {
                        $rmSurvivors += [PSCustomObject]@{
                            ProcessName = $rp.Name; PID = $rp.PID
                            HandleType = "RMSurvivor"; TargetPath = $Path
                        }
                        Write-DebugLog "  RM survivor (still running): $($rp.Name) PID=$($rp.PID)"
                    }
                }
            } else {
                # User declined graceful shutdown — all closable processes are survivors
                foreach ($rp in $closable) {
                    $rmSurvivors += [PSCustomObject]@{
                        ProcessName = $rp.Name; PID = $rp.PID
                        HandleType = "RMSurvivor"; TargetPath = $Path
                    }
                }
            }
        }
    }
    [RestartManager]::RmEndSession($sessionHandle) | Out-Null
}

# ============================================================
# Phase 2: handle.exe + module scan
# ============================================================
Write-Host "`nPhase 2: Detecting remaining locks..." -ForegroundColor Cyan
Write-DebugLog "Phase 2 started"

$handleExe = Get-HandleExePath
if (-not $handleExe) {
    Show-Message -Text "handle.exe not found in:`n$BinDir" -Icon Error
    throw "handle.exe not found"
}

$allLocks = @()

# 2a: handle.exe scan
$handleLocks = Invoke-HandleScan -TargetPath $Path -HandleExe $handleExe
Write-DebugLog "  handle.exe: $($handleLocks.Count) handle(s)"
$allLocks += $handleLocks

# 2b: module scan via tasklist /m (works for both 32-bit and 64-bit processes)
$moduleLocks = Get-LockingModules -TargetPath $Path -IsFolder $isFolder
Write-DebugLog "  Module scan: $($moduleLocks.Count) loaded module(s)"
$allLocks += $moduleLocks

# 2c: RM survivors (processes RmShutdown failed to close, or user declined)
if ($rmSurvivors.Count -gt 0) {
    Write-DebugLog "  RM survivors: $($rmSurvivors.Count) process(es) still holding locks"
    $allLocks += $rmSurvivors
}

# Deduplicate by PID
$lockPids = $allLocks | Select-Object -ExpandProperty PID -Unique
Write-DebugLog "  Total unique locking PIDs: $($lockPids.Count)"
$allLocks | ForEach-Object { Write-DebugLog "    Lock: $($_.ProcessName) PID=$($_.PID) [$($_.HandleType)] -> $($_.TargetPath)" }

if ($lockPids.Count -eq 0) {
    $result = Show-Message -Text "No locking processes detected.`n`nThe item may be locked by:`n- Explorer (preview pane / open window)`n- A kernel driver or antivirus`n- A process running as another user`n`nRestart Explorer? Or mark for delete-on-reboot?" -Buttons YesNoCancel -Icon Question
    if ($result -eq "Yes") {
        Stop-Process -Name "explorer" -Force -ErrorAction SilentlyContinue
        Start-Sleep 2
        Start-Process "explorer.exe"
        Show-Message -Text "Explorer restarted. Try your operation again."
    } elseif ($result -eq "Cancel") {
        return
    } else {
        # No = fall through to delete-on-reboot
    }
}

# ============================================================
# Phase 3: Kill locking processes
# ============================================================
if ($lockPids.Count -gt 0) {
    Write-Host "`nPhase 3: Terminating locks..." -ForegroundColor Cyan

    $targets = @()
    foreach ($pidVal in $lockPids) {
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

    if ($targets.Count -gt 0) {
        $list = ($targets | ForEach-Object { "  - $($_.ProcessName).exe (PID $($_.PID))" }) -join "`n"
        if ((Show-Message -Text "Terminate $($targets.Count) process(es)?`n`n$list`n`nUnsaved data will be lost." -Buttons YesNo -Icon Warning) -ne "Yes") {
            Write-DebugLog "  User cancelled termination"
        } else {
            $killed = @(); $failed = @()
            foreach ($t in $targets) {
                try { Stop-Process -Id $t.PID -Force -ErrorAction Stop; $killed += $t }
                catch { $failed += "$($t.ProcessName) (PID $($t.PID)): $_" }
            }
            Write-DebugLog "  Killed: $($killed.Count), Failed: $($failed.Count)"

            # Restart Explorer if killed
            if ($killed | Where-Object { $_.ProcessName -eq "explorer" }) {
                Start-Sleep 1
                if (-not (Get-Process -Name "explorer" -ErrorAction SilentlyContinue)) { Start-Process "explorer.exe" }
            }
            Start-Sleep -Seconds 1
        }
    }
}

# ============================================================
# Phase 4: Verify + delete-on-reboot fallback
# ============================================================
Write-Host "`nPhase 4: Verifying..." -ForegroundColor Cyan
Write-DebugLog "Phase 4: re-scanning"

$verifyLocks = Invoke-HandleScan -TargetPath $Path -HandleExe $handleExe
$verifyModules = Get-LockingModules -TargetPath $Path -IsFolder $isFolder
$remaining = @($verifyLocks) + @($verifyModules)
$remainingPids = $remaining | Select-Object -ExpandProperty PID -Unique

Write-DebugLog "  Remaining after kill: $($remainingPids.Count) PID(s)"

if ($remainingPids.Count -eq 0) {
    Show-Message -Text "All locks released successfully.`n`nTry your operation again." -Icon Information
    return
}

# Locks still remain — offer delete-on-reboot
$remList = ($remaining | Group-Object PID | ForEach-Object {
    $p = $_.Group[0]
    "  - $($p.ProcessName) (PID $($p.PID))"
}) -join "`n"

$choice = Show-Message -Text "Locks still held by:`n$remList`n`nThese processes resist termination (may auto-restart).`n`nMark the item for deletion on next reboot?" -Buttons YesNo -Icon Warning

if ($choice -eq "Yes") {
    $ok = [Kernel32]::MoveFileEx($Path, $null, [Kernel32]::MOVEFILE_DELAY_UNTIL_REBOOT)
    if ($ok) {
        Write-DebugLog "  Marked for delete-on-reboot: $Path"
        Show-Message -Text "Successfully marked for deletion on next reboot.`n`nThe item will be removed automatically when Windows restarts." -Icon Information
    } else {
        $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-DebugLog "  MoveFileEx failed, error=$err"
        Show-Message -Text "Failed to mark for delete-on-reboot (error $err).`n`nYou may need to run as Administrator." -Icon Error
    }
}

} # end Main

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
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    try { Show-Message -Text "Error:`n$errMsg`n`nDebug log:`n$DebugLog" -Icon Error } catch { }
} finally {
    Write-Host "`nDebug log: $DebugLog" -ForegroundColor Yellow
    Read-Host "Press Enter to close"
}
