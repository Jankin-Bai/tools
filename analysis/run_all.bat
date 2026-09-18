@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "BASE=%~dp0"
set "PY="
set "PYARGS="
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"

rem Optional override: PYTHON_EXE must be an executable path, without quotes.
if defined PYTHON_EXE if exist "%PYTHON_EXE%" set "PY=%PYTHON_EXE%"
if defined PY goto python_ready
set "MANAGED=%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
if exist "%MANAGED%" set "PY=%MANAGED%"
if defined PY goto python_ready
where py >nul 2>&1
if not errorlevel 1 (
    set "PY=py"
    set "PYARGS=-3"
    goto python_ready
)
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto python_ready
echo [ERROR] Python 3.10 or newer is required. Set PYTHON_EXE to its path.
exit /b 2

:python_ready
"%PY%" %PYARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 2)"
if errorlevel 1 (
    echo [ERROR] Python is unavailable or too old. Set PYTHON_EXE to Python 3.10+.
    exit /b 2
)
if not defined SESSIONS_ROOT set "SESSIONS_ROOT=%LOCALAPPDATA%\Doubao\User Data\Default\.doubao\agent_mode\workspace\.sessions"
if not exist "%SESSIONS_ROOT%\" (
    echo [ERROR] Source directory not found. Set SESSIONS_ROOT to your .sessions directory.
    exit /b 2
)
if not exist "%BASE%trajectory_stats.py" exit /b 2
if not exist "%BASE%trend.py" exit /b 2

echo Doubao analysis v2 - source files are read-only.
echo Output: "%BASE%reports"
echo Visible-text estimates are NOT billed token usage.
if "%~1"=="" goto sample_mode
if /i "%~1"=="--all" goto all_mode
if /i "%~1"=="--sample-known" goto sample_mode
echo Usage: run_all.bat [--sample-known ^| --all]
echo Default: the previously approved five-agent sample only.
exit /b 2

:sample_mode
"%PY%" %PYARGS% "%BASE%trajectory_stats.py" --sample-known --root "%SESSIONS_ROOT%" --out "%BASE%reports"
if errorlevel 1 goto failed
goto render

:all_mode
"%PY%" %PYARGS% "%BASE%trajectory_stats.py" --all --root "%SESSIONS_ROOT%" --out "%BASE%reports"
if errorlevel 1 goto failed

:render
"%PY%" %PYARGS% "%BASE%trend.py" --reports "%BASE%reports" --out "%BASE%reports\trend.html"
if errorlevel 1 goto failed
echo [OK] Dashboard: "%BASE%reports\trend.html"
echo [OK] Data: "%BASE%reports\stats_latest.json"
echo Open trend.html in your browser. No server or internet is required.
exit /b 0

:failed
echo [ERROR] Analysis did not finish. Check the error above.
echo Previous reports, if present, must not be treated as this run's results.
exit /b 1
