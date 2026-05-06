@echo off
REM Build CHAPPIE as a standalone Windows app (PyInstaller).
REM Produces:  dist\CHAPPIE\CHAPPIE.exe   (and supporting files)

setlocal
cd /d "%~dp0\.."

if not exist ".venv" (
  echo No .venv found. Run scripts\setup.bat first.
  exit /b 1
)

call .venv\Scripts\activate.bat

echo ==^> Ensuring build deps...
python -m pip install --quiet --upgrade pip pyinstaller pywebview

if exist "build" rmdir /s /q build
if exist "dist"  rmdir /s /q dist

echo ==^> Running PyInstaller (this takes 1-3 minutes)...
python -m PyInstaller --noconfirm --clean chappie.spec
if errorlevel 1 (
  echo Build failed. See output above.
  exit /b 1
)

echo.
echo Done. App is at:  dist\CHAPPIE\CHAPPIE.exe
echo Make a desktop shortcut, or zip the dist\CHAPPIE folder for distribution.
endlocal
