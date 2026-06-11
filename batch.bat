@echo off
setlocal
title MarkItDown - Batch Convert

cd /d "%~dp0"

set "APP_NAME=MarkItDown"
set "VENV_PY=.venv\Scripts\python.exe"
set "STAMP_FILE=.deps_installed"
set "INPUT_DIR=%~1"
set "OUTPUT_DIR=%~2"

if "%INPUT_DIR%"=="" set "INPUT_DIR=input"
if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=output"

echo.
echo Batch converting %APP_NAME% files...
echo Input:  %INPUT_DIR%
echo Output: %OUTPUT_DIR%
echo.

if not exist "%INPUT_DIR%" (
    mkdir "%INPUT_DIR%" >nul 2>&1
    echo [OK] Created %INPUT_DIR%.
    echo Drop files into that folder, then run batch.bat again.
    echo.
    pause
    exit /b 0
)

if not exist "%VENV_PY%" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. Install Python 3.10+ and try again.
        pause
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if(-not (Test-Path '.deps_installed')){ exit 1 }; if((Get-Item '.deps_installed').LastWriteTime -lt (Get-Item 'requirements.txt').LastWriteTime){ exit 1 }; exit 0"
if errorlevel 1 (
    echo Installing dependencies into .venv...
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo installed>"%STAMP_FILE%"
)

"%VENV_PY%" "batch_convert.py" "%INPUT_DIR%" "%OUTPUT_DIR%" --engine academic
set "BATCH_EXIT=%ERRORLEVEL%"

echo.
if "%BATCH_EXIT%"=="0" (
    echo [OK] Batch conversion finished. See %OUTPUT_DIR%\batch-results.json for details.
) else (
    echo [WARN] Batch conversion finished with errors. See %OUTPUT_DIR%\batch-results.json for details.
)
echo.
pause
exit /b %BATCH_EXIT%
