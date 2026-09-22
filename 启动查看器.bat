@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
title MCAP Video Viewer
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo.
echo   ============================================================
echo     MCAP Video Viewer  ^(Windows desktop^)
echo   ============================================================
echo.

rem PowerShell 5.1 is built into every supported Windows 10 release.
rem Keep delayed expansion disabled so paths containing ! remain intact.
set "RUNTIME_RESULT=%TEMP%\mcapviewer-runtime-%RANDOM%-%RANDOM%.txt"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" -ResultFile "%RUNTIME_RESULT%"
if errorlevel 1 (
    del /q "%RUNTIME_RESULT%" >nul 2>&1
    echo.
    echo   Startup failed. See startup-error.log for details.
    echo.
    pause
    exit /b 1
)

if not exist "%RUNTIME_RESULT%" (
    echo   [X] Bootstrap did not return a runtime path.
    pause
    exit /b 1
)
set /p "RPYW="<"%RUNTIME_RESULT%"
del /q "%RUNTIME_RESULT%" >nul 2>&1
if not exist "%RPYW%" (
    echo   [X] runtime validation succeeded but pythonw.exe is missing.
    pause
    exit /b 1
)

echo   Starting the viewer...
start "" "%RPYW%" "%~dp0desktop.py" %*
if errorlevel 1 (
    echo   [X] Failed to launch the desktop application.
    pause
    exit /b 1
)
exit /b 0
