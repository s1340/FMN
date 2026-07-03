@echo off
title Hermes Vault
echo.
echo  Hermes Vault -- starting...
echo.

call C:\Users\User\miniforge3\Scripts\activate.bat G:\miniconda_installed\envs\llmstate
if errorlevel 1 (
    echo  ERROR: failed to activate conda env
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
set MEMORY_VAULT_ROOT=C:\Users\User\Documents\Obsidian Vault

rem Open browser after 2s delay (server needs a moment to bind)
start /b "" cmd /c "ping -n 3 127.0.0.1 >nul && start http://localhost:5173"

python G:\LLM\memory\vault_viz.py --port 5173

echo.
echo  Server stopped.
pause
