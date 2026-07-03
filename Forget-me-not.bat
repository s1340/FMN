@echo off
rem Forget-me-not — double-click to open the memory panel.
rem First run shows a friendly setup page; after that, the panel itself.
cd /d "%~dp0"
start "" http://127.0.0.1:5173
python vault_viz.py
if errorlevel 1 (
  echo.
  echo Something didn't start. Most often this means Python isn't installed:
  echo   1. Get it from https://python.org/downloads  ^(check "Add to PATH"^)
  echo   2. Double-click this file again.
  echo Then, one time only, in this folder run:  pip install -r requirements.txt
  pause
)
