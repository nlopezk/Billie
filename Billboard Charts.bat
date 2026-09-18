@echo off
REM ===========================================================================
REM  Billboard Charts -- double-click launcher
REM
REM  Starts the local Django server and opens the browser, so the tool can be
REM  used without VS Code and without typing anything into a terminal.
REM
REM  This window IS the server. Closing it stops the tool. That is deliberate:
REM  a hidden background server is easy to forget about and end up running
REM  twice, which then fails with "port already in use".
REM ===========================================================================

setlocal

REM %~dp0 is the folder this .bat lives in, with a trailing backslash. Using it
REM means the shortcut works no matter which folder Windows starts it from --
REM manage.py is only findable from the project root.
cd /d "%~dp0"

title Billboard Charts  --  close this window to stop

set "URL=http://127.0.0.1:8000/"

REM ---------------------------------------------------------------------------
REM  Find Python. "py" is the Windows Python launcher, installed alongside
REM  Python itself and the most reliable way to find it from Explorer (whose
REM  PATH can differ from a terminal's). Falls back to plain "python".
REM ---------------------------------------------------------------------------
set "PYEXE=py"
where py >nul 2>&1 || set "PYEXE=python"

REM ---------------------------------------------------------------------------
REM  Is a server already listening on 8000? If so, don't start a second one --
REM  that just fails. Open the browser at the running one and quit.
REM  The PowerShell call exits 0 when the port is in use, 1 when it's free.
REM ---------------------------------------------------------------------------
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo.
    echo  The chart tool is already running -- opening it in your browser.
    echo.
    start "" "%URL%"
    timeout /t 3 /nobreak >nul
    exit /b 0
)

echo.
echo   Billboard Charts
echo   ----------------
echo   Opening %URL% in your browser...
echo.
echo   KEEP THIS WINDOW OPEN while you work.
echo   Close it when you're done to stop the server.
echo.

REM ---------------------------------------------------------------------------
REM  Open the browser a few seconds from now, in a separate hidden process, so
REM  the server below has time to come up first. Opening it immediately would
REM  land on a "can't reach this page" error. This runs in parallel because the
REM  next command blocks for as long as the server lives.
REM ---------------------------------------------------------------------------
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%URL%'"

REM ---------------------------------------------------------------------------
REM  --noreload runs a single process instead of Django's usual autoreloading
REM  pair. For a data-entry launcher that's what you want: closing this window
REM  reliably stops everything, with no orphaned child process left holding
REM  port 8000. The trade-off -- code edits need a restart -- doesn't matter
REM  when you're entering charts rather than changing the program.
REM ---------------------------------------------------------------------------
"%PYEXE%" manage.py runserver 127.0.0.1:8000 --noreload

REM Only reached if the server exits or fails to start. pause keeps the window
REM open so an error message is readable instead of vanishing instantly.
echo.
echo   The server has stopped.
echo.
pause
