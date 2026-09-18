@echo off
chcp 65001 >nul

echo =========================================
echo   Git Pull (Discard Local Changes)
echo =========================================
echo.

cd /d "%~dp0"

echo [INFO] Discarding all local changes...
git reset --hard HEAD
if %errorlevel% neq 0 (
    echo [ERROR] Failed to reset local changes.
    pause
    exit /b 1
)

echo [INFO] Pulling latest from remote...
git pull
if %errorlevel% neq 0 (
    echo [ERROR] git pull failed.
    pause
    exit /b 1
)

echo.
echo [OK] Done. Local code is now up to date.
pause
