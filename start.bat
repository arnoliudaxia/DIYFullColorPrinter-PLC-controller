@echo off
chcp 65001 >nul
REM PLC Device Remote Control Launcher
REM Uses uv to run, auto-handles virtual environment

echo =========================================
echo   PLC Device Remote Control
echo =========================================
echo.

REM --- find uv ---
set "UV_CMD="
where uv >nul 2>nul
if %errorlevel% equ 0 (
    set "UV_CMD=uv"
    goto :found_uv
)

REM Try known Kimi Work uv paths
if exist "%USERPROFILE%\AppData\Roaming\kimi-desktop\daimon-bundle\runtime\uv\uv.exe" (
    set "UV_CMD=%USERPROFILE%\AppData\Roaming\kimi-desktop\daimon-bundle\runtime\uv\uv.exe"
    goto :found_uv
)
if exist "%USERPROFILE%\.cargo\bin\uv.exe" (
    set "UV_CMD=%USERPROFILE%\.cargo\bin\uv.exe"
    goto :found_uv
)
if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" (
    set "UV_CMD=%LOCALAPPDATA%\Programs\uv\uv.exe"
    goto :found_uv
)
echo [ERROR] uv not found. Please install uv first:
echo   https://docs.astral.sh/uv/getting-started/installation/
echo.
pause
exit /b 1

:found_uv
echo [INFO] uv found: %UV_CMD%
%UV_CMD% --version
echo.

REM --- verify device_control.py ---
if not exist "%~dp0device_control.py" (
    echo [ERROR] device_control.py not found in current directory.
    echo   Please run this script from the same folder as device_control.py
    echo.
    pause
    exit /b 1
)

echo [INFO] Starting device_control.py ...
echo.

REM --- run ---
cd /d "%~dp0"
%UV_CMD% run python device_control.py

echo.
echo [INFO] Program exited.
pause
