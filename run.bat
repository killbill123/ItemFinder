@echo off
REM ItemFinder launcher - first run installs everything (internet needed once).
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python is not installed. Get Python 3.11 or 3.12 from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" during install, then run this file again.
  pause & exit /b 1
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo === First-time setup: installing ItemFinder (5-10 minutes, one time only) ===
  python -m venv .venv || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  REM CPU version of PyTorch (works on every PC). For an NVIDIA GPU see README.
  ".venv\Scripts\python.exe" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
)
start "" ".venv\Scripts\pythonw.exe" app.py
