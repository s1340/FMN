@echo off
rem Forget-me-not — double-click to open. No terminal stays behind:
rem the memory engine runs windowless (pythonw), the browser opens, this
rem window closes itself. To stop it later, use "Close" inside the panel.
cd /d "%~dp0"

where pythonw >nul 2>nul || goto nopython

rem start the engine with no visible window, as its own process
start "" pythonw vault_viz.py

rem give it a breath to wake, then open the panel and vanish
ping -n 2 127.0.0.1 >nul
start "" http://127.0.0.1:5173
exit

:nopython
echo.
echo   Forget-me-not needs Python — a free, safe program it runs on.
echo.
echo   1. Get it here:  https://python.org/downloads
echo      During install, TICK the box "Add Python to PATH".
echo   2. Then just double-click Forget-me-not again.
echo.
echo   (One time only, it may ask you to run:  pip install -r requirements.txt)
echo.
pause
