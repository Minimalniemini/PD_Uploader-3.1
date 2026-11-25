@echo off
setlocal
cd /d "%~dp0"
py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.11 from python.org, then run this file again.
  pause
  exit /b 1
)
py -3.11 -m venv .venv
if errorlevel 1 goto failed
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
python -m pip install -e .
if errorlevel 1 goto failed
python -m unittest discover -s tests
if errorlevel 1 goto failed
pyinstaller --noconfirm control_room_uploader.spec
if errorlevel 1 goto failed
echo.
echo Build complete: dist\PDUploader-0.3.1\PDUploader-0.3.1.exe
echo Copy the WHOLE PDUploader-0.3.1 folder, not just the EXE.
pause
exit /b 0
:failed
echo Build failed. Please keep this window open to read the error.
pause
exit /b 1
