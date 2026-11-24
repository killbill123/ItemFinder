@echo off
REM Optional local build. Normally GitHub Actions builds the installer for you.
REM Needs: run.bat run once (creates .venv), and Inno Setup 6 for the installer step.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" ( echo Run run.bat once first. & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install pyinstaller || (pause & exit /b 1)
".venv\Scripts\pyinstaller.exe" --noconfirm ItemFinder.spec || (pause & exit /b 1)
".venv\Scripts\python.exe" -c "import subprocess,sys;sys.exit(subprocess.call([r'dist\ItemFinder\ItemFinder.exe','--selftest','selftest.txt']))"
type selftest.txt
echo.
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% installer\ItemFinder.iss && echo Installer: installer\Output\
) else (
  echo App built in dist\ItemFinder. Install Inno Setup 6 from https://jrsoftware.org/isdl.php to also build the installer.
)
pause
