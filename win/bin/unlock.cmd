@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\Unlock-Folder\Unlock-Folder.ps1" %* -NoPause

