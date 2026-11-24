@echo off
rem Quick start for a machine where the project is ALREADY installed (WSL, Docker, containerlab, ~/slb).
rem It does not install anything and does not touch the rest of the PC. First time on a machine? Run start.bat instead.
rem What it does: starts the lab and the UI inside WSL, then opens the page. Keep the black window open; closing it stops the UI.
setlocal
wsl --cd ~/slb -e test -f ui/start.sh
if errorlevel 1 (
  echo The project is not installed in WSL yet. Run start.bat once, then use quick_start.bat.
  pause
  exit /b 1
)
echo Starting the lab and the UI. This takes about a minute. Close this window to stop the UI.
start "" /b cmd /c "timeout /t 45 >nul & start http://127.0.0.1:8765/"
wsl --cd ~/slb -e bash ui/start.sh