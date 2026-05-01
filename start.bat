@echo off
title MD_CREATOR
echo.
echo  ================================
echo   MD_CREATOR - Starting up...
echo  ================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

:: Install deps if needed
if not exist ".deps_installed" (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo. > .deps_installed
)

:: Open browser after a short delay, then start server
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"
echo Server running at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
python server.py
