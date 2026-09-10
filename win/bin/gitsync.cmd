@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\Git-Sync\Git-Sync.ps1" %* -NoPause

