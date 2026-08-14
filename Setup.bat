@echo off
title WishWheel Support Bot - Setup
color 0A

echo ========================================
echo    WISHWHEEL SUPPORT BOT - SETUP
echo ========================================
echo.

REM ============================================================
REM STEP 1: Python check
REM ============================================================

echo Checking Python installation...
echo.

python --version >nul 2>&1

if %errorlevel% neq 0 (
    echo Python not found.
    echo.
    echo Please install Python 3.10 or higher.
    echo Download from: https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Check "Add Python to PATH" during install.
    echo.
    start "" "https://www.python.org/downloads/"
    echo.
    echo After installing Python, run this file again.
    echo.
    pause
    exit /b 1
)

python --version
echo Python found. OK
echo.

REM ============================================================
REM STEP 2: pip check
REM ============================================================

echo Checking pip...
echo.

python -m pip --version >nul 2>&1

if %errorlevel% neq 0 (
    echo pip not found. Installing...
    echo.
    python -m ensurepip --upgrade
    if %errorlevel% neq 0 (
        echo Failed to install pip.
        echo Please reinstall Python from python.org
        echo.
        pause
        exit /b 1
    )
)

echo pip found. OK
echo.

REM ============================================================
REM STEP 3: pip upgrade
REM ============================================================

echo Upgrading pip...
echo.

python -m pip install --upgrade pip --quiet

echo pip upgraded. OK
echo.

REM ============================================================
REM STEP 4: Install requirements
REM ============================================================

echo Installing python-telegram-bot...
echo This may take a minute, please wait...
echo.

python -m pip install "python-telegram-bot[job-queue]>=20.0"

if %errorlevel% neq 0 (
    echo.
    echo Failed to install python-telegram-bot.
    echo Check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo python-telegram-bot installed. OK
echo.

REM ============================================================
REM STEP 5: BOT_TOKEN setup
REM ============================================================

echo ========================================
echo BOT TOKEN SETUP
echo ========================================
echo.
echo Get your token from @BotFather on Telegram.
echo.

if defined BOT_TOKEN (
    echo BOT_TOKEN is already set.
    echo.
    set /p CHANGE_TOKEN="Change token? (y/n): "
    if /i "%CHANGE_TOKEN%"=="y" goto ASK_TOKEN
    goto TOKEN_DONE
)

:ASK_TOKEN
echo.
set /p USER_TOKEN="Paste your BOT_TOKEN here and press Enter: "

if "%USER_TOKEN%"=="" (
    echo.
    echo No token entered.
    echo You can add it later in bot.py file.
    echo.
    goto TOKEN_DONE
)

setx BOT_TOKEN "%USER_TOKEN%" >nul

if %errorlevel% equ 0 (
    echo.
    echo Token saved successfully.
) else (
    echo.
    echo Could not save permanently.
    echo Setting for current session only.
    set BOT_TOKEN=%USER_TOKEN%
)

:TOKEN_DONE

REM ============================================================
REM Create run_bot.bat
REM ============================================================

echo.
echo Creating run_bot.bat...

(
echo @echo off
echo title WishWheel Support Bot
echo color 0A
echo echo ========================================
echo echo      WISHWHEEL SUPPORT BOT
echo echo ========================================
echo echo.
echo if not defined BOT_TOKEN ^(
echo     echo BOT_TOKEN not set!
echo     echo Run setup.bat first.
echo     echo.
echo     pause
echo     exit /b 1
echo ^)
echo echo Bot is starting...
echo echo Press Ctrl+C to stop.
echo echo.
echo python bot.py
echo echo.
echo echo Bot stopped.
echo pause
) > run_bot.bat

echo run_bot.bat created. OK
echo.

REM ============================================================
REM SUMMARY
REM ============================================================

echo ========================================
echo           SETUP COMPLETE
echo ========================================
echo.
echo Python              : OK
echo pip                 : OK
echo python-telegram-bot : OK
echo run_bot.bat         : Created
echo.
echo ========================================
echo HOW TO RUN THE BOT
echo ========================================
echo.
echo Double-click run_bot.bat to start bot.
echo.
echo ========================================
echo.

set /p START_NOW="Start the bot now? (y/n): "

if /i "%START_NOW%"=="y" (
    echo.
    echo Starting bot...
    echo.
    start "WishWheel Bot" cmd /k "python bot.py"
)

echo.
echo Done.
echo.
pause
exit /b 0