@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
title Build MCAP Viewer EXE
cd /d "%~dp0"

echo Building the standalone Windows EXE. This can take several minutes...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1"
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)
echo.
echo The EXE is ready in the release folder.
pause
