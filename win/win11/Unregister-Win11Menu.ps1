<#
.SYNOPSIS
    Unregister MyTools from the Windows 11 modern context menu.

.DESCRIPTION
    Removes the sparse package registration and optionally the certificate.
    The classic "My Tools" cascading menu is unaffected.
#>

[CmdletBinding()]
param(
    [switch]$RemoveCert   # Also remove the self-signed certificate from Trusted Root
)

$ErrorActionPreference = "Stop"
$PackageName = "MyTools.Framework"
$Publisher   = "CN=MyTools"
$Win11Dir    = $PSScriptRoot
$CertPath    = Join-Path $Win11Dir "MyTools.pfx"

Write-Host "[1/2] Removing sparse package..." -ForegroundColor Cyan
$pkg = Get-AppxPackage -Name $PackageName -ErrorAction SilentlyContinue
if ($pkg) {
    Remove-AppxPackage -Package $pkg.PackageFullName
    Write-Host "    [+] Package removed: $($pkg.PackageFullName)" -ForegroundColor Green
} else {
    Write-Host "    Package not registered (nothing to remove)" -ForegroundColor DarkGray
}

if ($RemoveCert) {
    Write-Host "[2/2] Removing certificate from Trusted Root..." -ForegroundColor Cyan
    $certs = Get-ChildItem -Path "Cert:\CurrentUser\Root" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq $Publisher }
    foreach ($c in $certs) {
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store(
            [System.Security.Cryptography.X509Certificates.StoreName]::Root,
            [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
        $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Remove($c)
        $store.Close()
        Write-Host "    [+] Removed cert: $($c.Thumbprint)" -ForegroundColor Green
    }
    # Also remove from My store
    $myCerts = Get-ChildItem -Path "Cert:\CurrentUser\My" -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq $Publisher }
    foreach ($c in $myCerts) {
        Remove-Item -Path $c.PSPath -Force
        Write-Host "    [+] Removed cert from My store: $($c.Thumbprint)" -ForegroundColor Green
    }
    if (Test-Path $CertPath) { Remove-Item $CertPath -Force; Write-Host "    [+] Deleted PFX backup" -ForegroundColor Green }
} else {
    Write-Host "[2/2] Certificate kept (use -RemoveCert to delete)" -ForegroundColor DarkGray
}

Write-Host "`nDone. Classic My Tools menu remains active." -ForegroundColor Green
