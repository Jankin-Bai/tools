<#
.SYNOPSIS
    MyTools.Common — shared module for all MyTools right-click tools.

.DESCRIPTION
    Provides Json-aware helpers used by every tool:
      - Show-Message     : MessageBox (no-op in Json mode, returns default)
      - Write-DebugLog   : timestamped log to %TEMP% (no console output in Json mode)
      - Test-Admin       : check if running elevated
      - New-ToolResult   : standardized result object factory
      - Invoke-Native    : run native command with ErrorActionPreference relaxation

    Usage in a tool script:
        Import-Module (Join-Path $PSScriptRoot "..\_shared\MyTools.Common.psm1") -Force
        Set-MyToolsMode -Json:$Json -DebugLogName "mytool"
        # ... then use Show-Message, Write-DebugLog, etc.
#>

$ErrorActionPreference = "Stop"

# Module-level state
$script:JsonMode = $false
$script:DebugLogPath = $null

Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop

function Set-MyToolsMode {
    <#
    .SYNOPSIS
        Configure the module for the current tool invocation.
    .PARAMETER Json
        If true, suppress all UI (MessageBox, console colors, Read-Host).
    .PARAMETER DebugLogName
        Base name for the debug log file in %TEMP% (e.g. "deptree" -> deptree-debug.log).
    #>
    param(
        [switch]$Json,
        [Parameter(Mandatory=$true)]
        [string]$DebugLogName
    )
    $script:JsonMode = $Json.IsPresent
    $script:DebugLogPath = Join-Path $env:TEMP "$DebugLogName-debug.log"
}

function Get-DebugLogPath {
    return $script:DebugLogPath
}

function Show-Message {
    <#
    .SYNOPSIS
        Show a MessageBox. In Json mode, returns the default button without UI.
    #>
    param(
        [string]$Text,
        [string]$Title = "MyTools",
        [System.Windows.Forms.MessageBoxButtons]$Buttons = [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]$Icon = [System.Windows.Forms.MessageBoxIcon]::Information
    )
    if ($script:JsonMode) {
        # Return the safe default button (cancel/no for destructive actions)
        $defaultMap = @{
            "OK"               = "OK"
            "OKCancel"         = "OK"
            "YesNo"            = "No"
            "YesNoCancel"      = "Cancel"
            "AbortRetryIgnore" = "Cancel"
            "RetryCancel"      = "Cancel"
        }
        $key = $Buttons.ToString()
        return if ($defaultMap.ContainsKey($key)) { $defaultMap[$key] } else { "OK" }
    }
    return [System.Windows.Forms.MessageBox]::Show($Text, $Title, $Buttons, $Icon)
}

function Write-DebugLog {
    <#
    .SYNOPSIS
        Write a timestamped line to the debug log. In non-Json mode, also echo to console.
    #>
    param([string]$Message)
    if (-not $script:DebugLogPath) {
        throw "Write-DebugLog called before Set-MyToolsMode"
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -Path $script:DebugLogPath -Value $line -Encoding UTF8
    if (-not $script:JsonMode) {
        Write-Host $line
    }
}

function Test-Admin {
    <#
    .SYNOPSIS
        Return $true if the current process is elevated.
    #>
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-ToolResult {
    <#
    .SYNOPSIS
        Create a standardized result object with common fields.
    #>
    param(
        [string]$Status = "running",
        [string]$Target = ""
    )
    return [ordered]@{
        status = $Status
        target = $Target
        errors = @()
    }
}

function Invoke-Native {
    <#
    .SYNOPSIS
        Run a native command with ErrorActionPreference temporarily relaxed to Continue.
        This prevents stderr warnings from being treated as terminating errors.
    .PARAMETER Command
        The executable to run.
    .PARAMETER Arguments
        Array of arguments.
    .PARAMETER EchoOutput
        If set, also echo each output line to the console (in addition to the debug log).
        Default: output goes only to the debug log; the caller decides what to display.
    .OUTPUTS
        Hashtable with Output (array of strings) and ExitCode (int).
    #>
    param(
        [Parameter(Mandatory=$true)]
        [string]$Command,
        [string[]]$Arguments = @(),
        [switch]$EchoOutput
    )
    $cmdLine = "$Command " + ($Arguments -join " ")
    Write-DebugLog "  > $cmdLine"

    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Command @Arguments 2>&1 | ForEach-Object { "$_" }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEAP
    }

    if ($output) {
        $output | ForEach-Object {
            Add-Content -Path $script:DebugLogPath -Value "    $_" -Encoding UTF8
            if ($EchoOutput -and -not $script:JsonMode) { Write-Host "    $_" }
        }
    }
    Write-DebugLog "  exit code: $exitCode"
    return @{ Output = $output; ExitCode = $exitCode }
}

Export-ModuleMember -Function @(
    "Set-MyToolsMode",
    "Get-DebugLogPath",
    "Show-Message",
    "Write-DebugLog",
    "Test-Admin",
    "New-ToolResult",
    "Invoke-Native"
)
