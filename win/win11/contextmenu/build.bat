@echo off
cd /d "%~dp0"
set "GPP=C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages\BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe\mingw64\bin\g++.exe"
"%GPP%" -shared -static-libgcc -static-libstdc++ -Wl,--enable-stdcall-fixup -o "..\MyToolsContextMenu.dll" MyToolsContextMenu.cpp config.cpp MyToolsContextMenu.def -lole32 -lshlwapi -luuid
echo Exit: %ERRORLEVEL%
