@echo off
setlocal
title MarkItDown - Stop

cd /d "%~dp0"

set "APP_NAME=MarkItDown"
set "PID_FILE=.server.pid"
set "QUIET=%~1"
set "STOPPED_COUNT=0"

if /i not "%QUIET%"=="/quiet" (
    echo.
    echo Stopping %APP_NAME%...
    echo.
)

if exist "%PID_FILE%" (
    set /p SERVER_PID=<"%PID_FILE%"
    for /f %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$killed=0; $pidText=$env:SERVER_PID; $root=(Resolve-Path '.').Path; if($pidText -match '^\d+$'){ $proc=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $pidText) -ErrorAction SilentlyContinue; if($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($root) -and $proc.CommandLine.Contains('server.py')){ taskkill.exe /PID ([int]$pidText) /T /F *> $null; if($LASTEXITCODE -eq 0){ $killed=1 } } }; Write-Output $killed"') do set "STOPPED_COUNT=%%C"
    del /f /q "%PID_FILE%" >nul 2>&1
)

for /f %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '.').Path; $count=[int]$env:STOPPED_COUNT; $procs=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($root) -and $_.CommandLine.Contains('server.py') }; foreach($p in $procs){ taskkill.exe /PID $p.ProcessId /T /F *> $null; if($LASTEXITCODE -eq 0){ $count++ } }; Write-Output $count"') do set "STOPPED_COUNT=%%C"

for /f %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '.').Path; $procs=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($root) -and $_.CommandLine.Contains('server.py') }; ($procs | Measure-Object).Count"') do set "REMAINING=%%C"

if /i not "%QUIET%"=="/quiet" (
    if "%REMAINING%"=="0" (
        if "%STOPPED_COUNT%"=="0" (
            echo [OK] No %APP_NAME% instance was running.
        ) else (
            echo [OK] %APP_NAME% is closed.
        )
    ) else (
        echo [WARN] Some %APP_NAME% processes may still be running.
    )
    echo.
)
exit /b 0
