@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\Dep-Tree\Dep-Tree.ps1" %* -NoPause

