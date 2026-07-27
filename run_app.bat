@echo off
setlocal
cd /d "%~dp0"

echo ====================================
echo    STOCKS WATCHER - Starting...
echo ====================================
echo.

REM The Python on PATH is not necessarily the one the dependencies are
REM installed on, and pythonw.exe hides the resulting ImportError, so probe
REM for an interpreter that can actually import everything gui.py needs.
where py >nul 2>&1
if errorlevel 1 (
    echo ERROR: The Python launcher 'py' was not found.
    echo Please install Python 3.8 or higher from python.org
    echo.
    pause
    exit /b 1
)

set "PYVER="
call :probe 3.13
if not defined PYVER call :probe 3.12
if not defined PYVER call :probe 3.14
if not defined PYVER call :probe 3

if not defined PYVER (
    echo ERROR: No Python install has the required packages.
    echo.
    echo Install them with:
    echo     py -3.13 -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Using Python %PYVER%. Starting Stocks Watcher GUI...
start "" pyw -%PYVER% gui.py
exit /b 0

:probe
py -%1 -c "import customtkinter, feedparser, yfinance, matplotlib, bs4, pytz, requests" >nul 2>&1
if not errorlevel 1 set "PYVER=%1"
goto :eof
