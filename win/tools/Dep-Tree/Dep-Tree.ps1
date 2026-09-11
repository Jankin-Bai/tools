<#
.SYNOPSIS
    Dependency Tree: analyze the dependency tree of a file (Makefile, Python source, Python packages).

.DESCRIPTION
    Launched from "My Tools > Dep Tree" right-click context menu on a file.
    Runs dep_tree.py to parse and display the full dependency tree.
    Supports: Makefile (*.mk, *.mak), Python source (*.py, import graph via pydeps+ast),
    Python installed packages (requirements.txt, pyproject.toml, via pipdeptree+importlib.metadata),
    Python function call graphs (*.py, via pyan3+ast).
    Output formats: text tree, JSON, Mermaid, Graphviz DOT, rendered PNG/SVG.

.PARAMETER Path
    Full path of the right-clicked file. Passed automatically as "%1".

.PARAMETER NoPause
    Skip the final Read-Host pause. Used by CLI .cmd shims and MCP.

.PARAMETER Json
    Machine-readable JSON output. No MessageBox, no console colors, no pause.

.PARAMETER ExtraArgs
    Additional arguments passed through to dep_tree.py (e.g. "--mermaid",
    "--dot", "--dirty", "--mode source --json", "--mode package --name requests --reverse").
    Used by submenu entries and CLI. Arguments with spaces in values must be quoted.

.NOTES
    Debug log: %TEMP%\dep-tree-debug.log
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Path,
    [switch]$NoPause,
    [switch]$Json,
    [string]$ExtraArgs = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent

Import-Module (Join-Path $ScriptDir "..\_shared\MyTools.Common.psm1") -Force
Set-MyToolsMode -Json:$Json -DebugLogName "dep-tree"

$DepTreePy = Join-Path $ScriptDir "dep_tree.py"

function Main {
    $result = New-ToolResult -Status "running" -Target $Path

    Write-DebugLog "=== Dep Tree started ==="
    Write-DebugLog "Path: $Path"
    Write-DebugLog "PSVersion: $($PSVersionTable.PSVersion)"
    Write-DebugLog "Mode: $(if ($Json) {'JSON'} elseif ($NoPause) {'CLI'} else {'Interactive'})"

    # --- Validate ---
    if (-not (Test-Path -LiteralPath $Path)) {
        $result.errors += "File not found: $Path"
        Show-Message -Text "File not found:`n$Path" -Title "Dep Tree" -Icon Error
        throw "File not found: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer) {
        $result.errors += "Target is a folder, not a file"
        Show-Message -Text "Dep Tree works on files only, not folders." -Title "Dep Tree" -Icon Error
        throw "Target is a folder, not a file"
    }
    if (-not (Test-Path -LiteralPath $DepTreePy)) {
        $result.errors += "dep_tree.py not found at: $DepTreePy"
        Show-Message -Text "dep_tree.py not found at:`n$DepTreePy" -Title "Dep Tree" -Icon Error
        throw "dep_tree.py not found"
    }

    # --- Check python (use py launcher, not bare python, to avoid KiCad/other python in PATH) ---
    $pyCheck = Invoke-Native -Command "py" -Arguments @("--version")
    if ($pyCheck.ExitCode -ne 0) {
        $result.errors += "py launcher not found in PATH"
        Show-Message -Text "Python launcher (py.exe) not found in PATH." -Title "Dep Tree" -Icon Error
        throw "python not found"
    }

    # --- Run dep_tree.py ---
    if (-not $Json) {
        Write-Host "`nAnalyzing: $Path" -ForegroundColor Cyan
        Write-Host "Using: $DepTreePy`n" -ForegroundColor DarkGray
    }

    # Force UTF-8 for Chinese text and box-drawing characters
    $oldOutputEncoding = [Console]::OutputEncoding
    $oldInputEncoding  = [Console]::InputEncoding
    $oldPythonIO       = $env:PYTHONIOENCODING
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = "utf-8"

    $pyArgs = @($DepTreePy, $Path)
    if ($Json) { $pyArgs += "--json" }
    if ($ExtraArgs) {
        # Split on spaces but keep quoted values intact (e.g. --python "C:\path with spaces\python.exe")
        $pyArgs += $ExtraArgs -split ' (?=(?:[^"]*"[^"]*")*[^"]*$)' |
            Where-Object { $_ -and $_.Trim() } |
            ForEach-Object { $_.Trim('"') }
    }
    if ($RemainingArgs) { $pyArgs += $RemainingArgs }
    $runResult = Invoke-Native -Command "py" -Arguments $pyArgs

    [Console]::OutputEncoding = $oldOutputEncoding
    [Console]::InputEncoding  = $oldInputEncoding
    $env:PYTHONIOENCODING = $oldPythonIO

    # --- Process output ---
    if ($Json) {
        if ($runResult.ExitCode -eq 0 -and $runResult.Output) {
            $result.status = "ok"
            $result["tree_json"] = ($runResult.Output -join "`n").Trim()
        } else {
            $result.status = "error"
            $result.errors += ($runResult.Output -join "`n")
        }
        Write-DebugLog "  exit code: $($runResult.ExitCode), status: $($result.status)"
    } else {
        if ($runResult.Output) { $runResult.Output | ForEach-Object { Write-Host $_ } }
        Write-DebugLog "  exit code: $($runResult.ExitCode)"
        if ($runResult.ExitCode -ne 0) {
            Show-Message -Text "Dep tree analysis failed (exit code $($runResult.ExitCode))." -Title "Dep Tree" -Icon Warning
        } else {
            Write-DebugLog "  Analysis completed successfully"
        }
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
    if (-not $result) { $result = New-ToolResult -Status "error" -Target $Path }
    $result.status = "error"
    if ($result.errors -notcontains $errMsg) { $result.errors += $errMsg }
    if (-not $Json) {
        Write-Host "`n=== ERROR ===" -ForegroundColor Red
        Write-Host $errMsg -ForegroundColor Red
        try { Show-Message -Text "Error:`n$errMsg`n`nDebug log:`n$(Get-DebugLogPath)" -Title "Dep Tree" -Icon Error } catch { }
    }
} finally {
    if ($Json) {
        $result | ConvertTo-Json -Depth 10
    } else {
        Write-Host "`nDebug log: $(Get-DebugLogPath)" -ForegroundColor Yellow
        if (-not $NoPause) { Read-Host "Press Enter to close" }
    }
}
