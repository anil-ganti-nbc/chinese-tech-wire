@echo off
setlocal enabledelayedexpansion
rem Manual, INTERACTIVE collection run for Chinese Tech Wire.
rem
rem The desktop dashboard launcher (main.py --gui, via
rem "_Launchers\Chinese Tech Wire Dashboard.cmd") never crawls anything on
rem its own just from being opened - opening the dashboard is always safe.
rem This script is the actual, discoverable way to populate/update the
rem local SQLite database on Windows: it runs the exact same
rem `main.py --full-once` production path the dashboard's own
rem "Run collector now" button and the (currently disabled) Task Scheduler
rem entry use. It writes the same data\ctw.db the dashboard reads from.
rem Output is kept on-screen AND appended to logs\ctw.log (and a
rem timestamped copy under logs\), so a fatal error is never hidden behind
rem a closed console window.
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo ERROR: Chinese Tech Wire virtual environment not found at:
    echo   %CD%\%PY%
    echo Run setup first from this repo directory, e.g.:
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%I"
set "LOGFILE=logs\collection-%STAMP%.log"

echo Chinese Tech Wire - manual collection run
echo Repo:     %CD%
echo Python:   %CD%\%PY%
echo Database: %CD%\data\ctw.db
echo Log:      %CD%\%LOGFILE%
echo (also appended to %CD%\logs\ctw.log)
echo.

rem Note: deliberately NOT "... 2>&1 | Tee-Object ...". Piping a native
rem process's merged stderr through PowerShell wraps every stderr line
rem (including this app's normal [WARNING]-level log lines, which Python's
rem logging module sends to stderr by default) as a NativeCommandError
rem record - it prints scary fake "error" noise for benign warnings AND
rem silently makes the wrapper's own exit code wrong. Start-Transcript
rem captures real-time console output (still fully visible here) without
rem going through a pipe, so it doesn't have either problem, and the
rem real python.exe exit code is propagated explicitly via $LASTEXITCODE.
powershell -NoProfile -Command "Start-Transcript -Path '%LOGFILE%' -Append | Out-Null; & '%PY%' main.py --full-once; $code = $LASTEXITCODE; Stop-Transcript | Out-Null; exit $code"
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo RESULT: collection completed ^(exit 0^). See the "finish id=... status="
    echo         line above - status=SUCCESS means the run completed cleanly;
    echo         status=PARTIAL means some sources failed even though the run
    echo         finished. Per-source failures are logged above. Full detail
    echo         also in %LOGFILE% and logs\ctw.log.
) else (
    echo RESULT: collection FAILED to complete ^(exit %EXITCODE%^). See
    echo         %LOGFILE% and the output above for the error.
)
echo.
echo Full log saved to: %CD%\%LOGFILE%
pause
exit /b %EXITCODE%
