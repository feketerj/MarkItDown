@echo off
setlocal
title MarkItDown - Stop

cd /d "%~dp0"

set "APP_NAME=MarkItDown"
set "PID_FILE=.server.pid"
set "QUIET=%~1"

if /i not "%QUIET%"=="/quiet" (
    echo.
    echo Stopping %APP_NAME%...
    echo.
)

if exist "%PID_FILE%" (
    set /p SERVER_PID=<"%PID_FILE%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$pidText=$env:SERVER_PID; $root=(Resolve-Path '.').Path; if($pidText -match '^\d+$'){ $proc=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $pidText) -ErrorAction SilentlyContinue; if($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($root) -and $proc.CommandLine.Contains('server.py')){ taskkill.exe /PID ([int]$pidText) /T /F | Out-Null } }"
    del /f /q "%PID_FILE%" >nul 2>&1
)

for /f %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '.').Path; $count=0; $procs=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($root) -and $_.CommandLine.Contains('server.py') }; foreach($p in $procs){ taskkill.exe /PID $p.ProcessId /T /F | Out-Null; $count++ }; Write-Output $count"') do set "STOPPED_COUNT=%%C"

for /f %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '.').Path; $procs=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($root) -and $_.CommandLine.Contains('server.py') }; ($procs | Measure-Object).Count"') do set "REMAINING=%%C"

if /i not "%QUIET%"=="/quiet" (
    if "%REMAINING%"=="0" (
        echo [OK] %APP_NAME% is closed.
    ) else (
        echo [WARN] Some %APP_NAME% processes may still be running.
    )
    echo.
)
exit /b 0
