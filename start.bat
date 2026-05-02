@echo off
setlocal
title MarkItDown - Start

cd /d "%~dp0"

set "APP_NAME=MarkItDown"
set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
set "APP_RELOAD=0"
set "APP_URL=http://%APP_HOST%:%APP_PORT%"
set "VENV_PY=.venv\Scripts\python.exe"
set "PID_FILE=.server.pid"
set "STAMP_FILE=.deps_installed"
set "LOCK_DIR=.start.lock"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$lock=Get-Item '.start.lock' -ErrorAction SilentlyContinue; if($lock -and $lock.LastWriteTime -lt (Get-Date).AddMinutes(-15)){ Remove-Item -LiteralPath '.start.lock' -Recurse -Force -ErrorAction SilentlyContinue }"
mkdir "%LOCK_DIR%" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] %APP_NAME% start is already in progress in this folder.
    echo If no start window is open, delete %LOCK_DIR% and try again.
    pause
    exit /b 1
)

echo.
echo Starting %APP_NAME% at %APP_URL%...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$url=$env:APP_URL + '/api/health'; $name=$env:APP_NAME; try { $h=Invoke-RestMethod -Uri $url -TimeoutSec 2; if($h.status -eq 'ok' -and $h.app_name -eq $name){ exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
    echo [OK] %APP_NAME% is already running at %APP_URL%.
    echo No new browser window was opened.
    echo.
    rmdir "%LOCK_DIR%" >nul 2>&1
    exit /b 0
)

if not exist "%VENV_PY%" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. Install Python 3.10+ and try again.
        rmdir "%LOCK_DIR%" >nul 2>&1
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
        rmdir "%LOCK_DIR%" >nul 2>&1
        pause
        exit /b 1
    )
    echo installed>"%STAMP_FILE%"
)

call stop.bat /quiet >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "$port=[int]$env:APP_PORT; $listeners=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if($listeners){ $pids=$listeners | Select-Object -ExpandProperty OwningProcess -Unique; Write-Host ('[ERROR] Port ' + $port + ' is already in use by PID(s): ' + (($pids | ForEach-Object { [string]$_ }) -join ', ')); exit 1 }; exit 0"
if errorlevel 1 (
    echo [ERROR] %APP_NAME% cannot start because %APP_URL% is not available.
    echo Close the process using port %APP_PORT%, then run start.bat again.
    rmdir "%LOCK_DIR%" >nul 2>&1
    pause
    exit /b 1
)

for /f %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '.').Path; $py=Join-Path $root '.venv\Scripts\python.exe'; $out=Join-Path $root '.server.log'; $err=Join-Path $root '.server.err.log'; $env:APP_NAME='%APP_NAME%'; $env:APP_HOST='%APP_HOST%'; $env:APP_PORT='%APP_PORT%'; $env:APP_RELOAD='%APP_RELOAD%'; $q=[char]34; $cmd='/d /c ' + $q + $q + $py + $q + ' server.py > ' + $q + $out + $q + ' 2> ' + $q + $err + $q + $q; $p=Start-Process -FilePath $env:ComSpec -ArgumentList $cmd -WorkingDirectory $root -WindowStyle Hidden -PassThru; $p.Id"') do set "SERVER_PID=%%I"

if "%SERVER_PID%"=="" (
    echo [ERROR] Failed to launch server process.
    rmdir "%LOCK_DIR%" >nul 2>&1
    pause
    exit /b 1
)

echo %SERVER_PID%>"%PID_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$url=$env:APP_URL + '/api/health'; $name=$env:APP_NAME; for($i=0; $i -lt 30; $i++){ try { $h=Invoke-RestMethod -Uri $url -TimeoutSec 2; if($h.status -eq 'ok' -and $h.app_name -eq $name){ exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
    echo [ERROR] Server did not become healthy in time.
    echo Check .server.log and .server.err.log in this folder.
    rmdir "%LOCK_DIR%" >nul 2>&1
    pause
    exit /b 1
)

start "" "%APP_URL%"

echo [OK] %APP_NAME% is open at %APP_URL%
echo.
echo Double-click stop.bat to close this app.
echo.
rmdir "%LOCK_DIR%" >nul 2>&1
exit /b 0
