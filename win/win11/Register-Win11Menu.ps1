<#
.SYNOPSIS
    Register MyTools into the Windows 11 modern context menu via Sparse Package.

.DESCRIPTION
    Windows 11's new (XAML Islands) context menu requires a packaged identity
    with the windows.fileExplorerContextMenus extension. Each menu item is a
    COM object implementing IExplorerCommand. This script:
      1. Generates logo assets
      2. Scans tools\*.json and generates a C++ config (CLSID per item)
      3. Compiles MyToolsContextMenu.dll (native COM DLL) with MinGW
      4. Generates AppxManifest.xml (desktop4 + com namespaces)
      5. Creates a self-signed cert and registers the sparse package

.NOTES
    Requires Windows 10 2004+ / Windows 11 and g++ (MinGW) in PATH.
    The classic "My Tools" cascading menu is unaffected.
#>

[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = "Stop"
$FrameworkRoot = Split-Path $PSScriptRoot -Parent
$Win11Dir      = $PSScriptRoot
$AssetsDir     = Join-Path $Win11Dir "assets"
$SrcDir        = Join-Path $Win11Dir "contextmenu"
$ManifestPath  = Join-Path $Win11Dir "AppxManifest.xml"
$CertPath      = Join-Path $Win11Dir "MyTools.pfx"
$CertPassword  = ConvertTo-SecureString "mytools" -AsPlainText -Force
$Publisher     = "CN=MyTools"
$PackageName   = "MyTools.Framework"
$PackageVersion = "1.0.0.0"
$DllName       = "MyToolsContextMenu.dll"

# ============================================================
# 1. Logos
# ============================================================
function New-LogoAsset {
    param([int]$Size, [string]$Path)
    if ((Test-Path $Path) -and -not $Force) { return }
    Add-Type -AssemblyName System.Drawing
    $bmp = New-Object System.Drawing.Bitmap($Size, $Size)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(0, 120, 215))
    $g.FillRectangle($brush, 0, 0, $Size, $Size)
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::White, [math]::Max(2, $Size / 16))
    $g.DrawLine($pen, $Size * 0.3, $Size * 0.7, $Size * 0.7, $Size * 0.3)
    $g.DrawEllipse($pen, $Size * 0.15, $Size * 0.55, $Size * 0.3, $Size * 0.3)
    $g.DrawEllipse($pen, $Size * 0.55, $Size * 0.15, $Size * 0.3, $Size * 0.3)
    $g.Dispose(); $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
}

Write-Host "[1/6] Generating logos..." -ForegroundColor Cyan
New-LogoAsset -Size 50  -Path (Join-Path $Win11Dir "StoreLogo.png")
New-LogoAsset -Size 150 -Path (Join-Path $Win11Dir "Square150x150Logo.png")
New-LogoAsset -Size 44  -Path (Join-Path $Win11Dir "Square44x44Logo.png")

# ============================================================
# 2. Scan tools -> menu items with CLSIDs
# ============================================================
Write-Host "[2/6] Scanning tools..." -ForegroundColor Cyan

$toolsDir = Join-Path $FrameworkRoot "tools"
$toolManifests = Get-ChildItem -Path $toolsDir -Recurse -Filter "tool.json" -ErrorAction SilentlyContinue

$items = @()
foreach ($tm in $toolManifests) {
    try { $tool = Get-Content $tm.FullName -Raw | ConvertFrom-Json } catch { continue }
    $toolDir = Split-Path $tm.FullName -Parent
    $scriptPath = Join-Path $toolDir $tool.Script
    if (-not (Test-Path $scriptPath)) { continue }

    if ($tool.Subcommands -and @($tool.Subcommands).Count -gt 0) {
        foreach ($sub in $tool.Subcommands) {
            $items += [ordered]@{
                Id       = "{0}-{1}" -f $tool.Name, $sub.Name
                Title    = "{0}: {1}" -f $tool.DisplayName, $sub.DisplayName
                Script   = $scriptPath
                ExtraArgs = $sub.Args
                Clsid    = [guid]::NewGuid().ToString("B").ToUpper()
            }
        }
    } else {
        $items += [ordered]@{
            Id       = $tool.Name
            Title    = $tool.DisplayName
            Script   = $scriptPath
            ExtraArgs = ""
            Clsid    = [guid]::NewGuid().ToString("B").ToUpper()
        }
    }
}
Write-Host "    $($items.Count) menu item(s) from $($toolManifests.Count) tool(s)" -ForegroundColor DarkGray

# ============================================================
# 3. Generate C++ config + compile COM DLL
# ============================================================
Write-Host "[3/6] Generating C++ config and compiling COM DLL..." -ForegroundColor Cyan

function Convert-GuidToCppInit([string]$guidStr) {
    $clean = $guidStr.Trim('{}')
    $parts = $clean.Split('-')
    $d4 = ($parts[3] + $parts[4]) -split '(..)' | Where-Object { $_ }
    return "{ 0x$($parts[0]), 0x$($parts[1]), 0x$($parts[2]), { 0x$($d4[0]), 0x$($d4[1]), 0x$($d4[2]), 0x$($d4[3]), 0x$($d4[4]), 0x$($d4[5]), 0x$($d4[6]), 0x$($d4[7]) } }"
}

# config.h
$parentClsid = "{8B9F1D2A-3C4E-4F5A-9B6D-7E8F0A1B2C3D}"
$parentCppInit = Convert-GuidToCppInit $parentClsid
$configH = @()
$configH += "#pragma once"
$configH += "#include <windows.h>"
$configH += ""
$configH += "struct ToolConfig {"
$configH += "    CLSID clsid;"
$configH += "    const wchar_t* title;"
$configH += "    const wchar_t* scriptPath;"
$configH += "    const wchar_t* extraArgs;"
$configH += "};"
$configH += ""
$configH += "#define TOOL_COUNT $($items.Count)"
$configH += "static const CLSID PARENT_CLSID = $parentCppInit;"
$configH += "extern const ToolConfig g_toolConfigs[TOOL_COUNT];"
$configH -join "`n" | Set-Content (Join-Path $SrcDir "config.h") -Encoding ASCII

# config.cpp
$configCpp = @()
$configCpp += '#include "config.h"'
$configCpp += ""
$configCpp += "const ToolConfig g_toolConfigs[TOOL_COUNT] = {"
for ($i = 0; $i -lt $items.Count; $i++) {
    $item = $items[$i]
    $cppInit = Convert-GuidToCppInit $item.Clsid
    $title = $item.Title -replace '\\', '\\' -replace '"', '\"'
    $script = $item.Script -replace '\\', '\\' -replace '"', '\"'
    $args = $item.ExtraArgs -replace '\\', '\\' -replace '"', '\"'
    $comma = if ($i -lt $items.Count - 1) { "," } else { "" }
    $configCpp += '    { ' + $cppInit + ', L"' + $title + '", L"' + $script + '", L"' + $args + '" }' + $comma
}
$configCpp += "};"
$configCpp -join "`n" | Set-Content (Join-Path $SrcDir "config.cpp") -Encoding ASCII

# Compile via build.bat (avoids PowerShell comma-escaping issues with -Wl,--enable-stdcall-fixup)
$dllPath = Join-Path $Win11Dir $DllName
$buildBat = Join-Path $SrcDir "build.bat"
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $buildBat 2>&1 | Out-Null
$ErrorActionPreference = $oldEAP
if (-not (Test-Path $dllPath)) { throw "DLL compilation failed" }
Write-Host "    DLL: $dllPath ($((Get-Item $dllPath).Length) bytes)" -ForegroundColor DarkGray

# Compile MyToolsLauncher.exe (native Win32, required by manifest Executable)
$launcherCpp = Join-Path $SrcDir "launcher.cpp"
$launcherExe = Join-Path $Win11Dir "MyToolsLauncher.exe"
if (Test-Path $launcherCpp) {
    $oldEAP2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $gxx -mwindows -o $launcherExe $launcherCpp -lshell32 2>&1 | Out-Null
    $ErrorActionPreference = $oldEAP2
    if (Test-Path $launcherExe) {
        Write-Host "    Launcher: $launcherExe ($((Get-Item $launcherExe).Length) bytes)" -ForegroundColor DarkGray
    } else {
        Write-Warning "Launcher compilation failed"
    }
}

# ============================================================
# 4. Generate AppxManifest.xml
# ============================================================
Write-Host "[4/6] Generating manifest..." -ForegroundColor Cyan

$X = @()
$X += '<?xml version="1.0" encoding="utf-8"?>'
$X += '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"'
$X += '         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"'
$X += '         xmlns:uap3="http://schemas.microsoft.com/appx/manifest/uap/windows10/3"'
$X += '         xmlns:desktop="http://schemas.microsoft.com/appx/manifest/desktop/windows10"'
$X += '         xmlns:uap10="http://schemas.microsoft.com/appx/manifest/uap/windows10/10"'
$X += '         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"'
$X += '         xmlns:desktop4="http://schemas.microsoft.com/appx/manifest/desktop/windows10/4"'
$X += '         xmlns:desktop5="http://schemas.microsoft.com/appx/manifest/desktop/windows10/5"'
$X += '         xmlns:com="http://schemas.microsoft.com/appx/manifest/com/windows10"'
$X += '         IgnorableNamespaces="uap uap3 desktop uap10 rescap desktop4 desktop5 com">'
$X += '  <Identity Name="{0}" ProcessorArchitecture="neutral" Publisher="{1}" Version="{2}" />' -f $PackageName, $Publisher, $PackageVersion
$X += '  <Properties>'
$X += '    <DisplayName>MyTools</DisplayName>'
$X += '    <PublisherDisplayName>MyTools</PublisherDisplayName>'
$X += '    <Logo>StoreLogo.png</Logo>'
$X += '    <uap10:AllowExternalContent>true</uap10:AllowExternalContent>'
$X += '  </Properties>'
$X += '  <Dependencies>'
$X += '    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.22000.0" MaxVersionTested="10.0.26200.0" />'
$X += '  </Dependencies>'
$X += '  <Capabilities>'
$X += '    <rescap:Capability Name="runFullTrust" />'
$X += '    <rescap:Capability Name="unvirtualizedResources" />'
$X += '  </Capabilities>'
$X += '  <Applications>'
$X += '    <Application Id="MyTools" Executable="MyToolsLauncher.exe" uap10:TrustLevel="mediumIL" uap10:RuntimeBehavior="win32App">'
$X += '      <uap:VisualElements AppListEntry="none" DisplayName="MyTools" Description="MyTools Framework" BackgroundColor="transparent"'
$X += '                          Square150x150Logo="Square150x150Logo.png" Square44x44Logo="Square44x44Logo.png" />'
$X += '      <Extensions>'
# Context menu extension — single parent "My Tools" verb with submenu
$parentClsidNoBraces = $parentClsid.Trim('{}')
$X += '        <desktop4:Extension Category="windows.fileExplorerContextMenus">'
$X += '          <desktop4:FileExplorerContextMenus>'
foreach ($itemType in @("*", "Directory", "Directory\Background")) {
    $X += '            <desktop5:ItemType Type="{0}">' -f $itemType
    $X += '              <desktop5:Verb Id="MyTools" Clsid="{0}" />' -f $parentClsidNoBraces
    $X += '            </desktop5:ItemType>'
}
$X += '          </desktop4:FileExplorerContextMenus>'
$X += '        </desktop4:Extension>'
# COM server extension — only the parent CLSID needs registration
$X += '        <com:Extension Category="windows.comServer">'
$X += '          <com:ComServer>'
$X += '            <com:SurrogateServer DisplayName="MyTools Context Menu">'
$X += '              <com:Class Id="{0}" Path="{1}" ThreadingModel="STA" />' -f $parentClsidNoBraces, $DllName
$X += '            </com:SurrogateServer>'
$X += '          </com:ComServer>'
$X += '        </com:Extension>'
$X += '      </Extensions>'
$X += '    </Application>'
$X += '  </Applications>'
$X += '</Package>'

$X | Set-Content -Path $ManifestPath -Encoding UTF8
Write-Host "    Manifest: $ManifestPath" -ForegroundColor DarkGray

# ============================================================
# 5. Certificate
# ============================================================
Write-Host "[5/6] Setting up certificate..." -ForegroundColor Cyan
$cert = Get-ChildItem -Path "Cert:\CurrentUser\Root" -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $Publisher } | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Publisher `
        -CertStoreLocation "Cert:\CurrentUser\My" -KeyUsage DigitalSignature `
        -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3") -NotAfter (Get-Date).AddYears(10)
    Export-PfxCertificate -Cert $cert -FilePath $CertPath -Password $CertPassword | Out-Null
    $rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store(
        [System.Security.Cryptography.X509Certificates.StoreName]::Root,
        [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
    $rootStore.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $rootStore.Add($cert); $rootStore.Close()
    Write-Host "    Certificate created and installed to Trusted Root" -ForegroundColor DarkGray
} else {
    Write-Host "    Certificate already exists" -ForegroundColor DarkGray
}

# ============================================================
# 6. Register
# ============================================================
Write-Host "[6/6] Registering sparse package..." -ForegroundColor Cyan
$existing = Get-AppxPackage -Name $PackageName -ErrorAction SilentlyContinue
if ($existing) { Remove-AppxPackage -Package $existing.PackageFullName -ErrorAction SilentlyContinue }

try {
    Add-AppxPackage -Register $ManifestPath -ExternalLocation $Win11Dir -ErrorAction Stop
    Write-Host "    [+] Sparse package registered" -ForegroundColor Green
} catch {
    Write-Host "    [!] Failed: $_" -ForegroundColor Red
    throw
}

# Also register parent CLSID in HKCU\CLSID (ensures COM activation works)
$dllFullPath = Join-Path $Win11Dir $DllName
$clsidKey = "HKCU:\Software\Classes\CLSID\$parentClsid"
[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Software\Classes\CLSID\$parentClsid") | Out-Null
Set-ItemProperty -Path $clsidKey -Name "(Default)" -Value "My Tools"
$inprocKey = "HKCU:\Software\Classes\CLSID\$parentClsid\InprocServer32"
[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Software\Classes\CLSID\$parentClsid\InprocServer32") | Out-Null
Set-ItemProperty -Path $inprocKey -Name "(Default)" -Value $dllFullPath
Set-ItemProperty -Path $inprocKey -Name "ThreadingModel" -Value "Apartment"
Write-Host "    [+] Parent CLSID registered in HKCU\CLSID" -ForegroundColor DarkGray

Write-Host ""
Write-Host "=== Win11 New Menu Registration Complete ===" -ForegroundColor Green
Write-Host "  Parent: My Tools (submenu with $($items.Count) tools)"
Write-Host "  Contexts: files, folders, folder background"
Write-Host "  Restart Explorer if items don't appear:"
Write-Host "  Stop-Process -Name explorer -Force; Start-Process explorer.exe"
