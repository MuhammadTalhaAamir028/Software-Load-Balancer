@echo off
rem Project 5 launcher for Windows. Double-click it, or run it from a terminal in this folder.
rem It finds Python (installing it with winget if needed) and runs start.py, which does everything.
setlocal
cd /d "%~dp0"

set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo Python was not found. Installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE="%LocalAppData%\Programs\Python\Python312\python.exe""
)
if not defined PYEXE (
  echo.
  echo Could not find or install Python. Install it from https://www.python.org/downloads/ and run start.bat again.
  pause
  exit /b 1
)

%PYEXE% "%~dp0start.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo start.py stopped with code %CODE%. Read the message above.
  pause
)
exit /b %CODE%
