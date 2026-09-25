@echo off
REM Build a standalone MRI_instructions.exe (run on Windows, needs Python 3 installed once,
REM from python.org with "tcl/tk" enabled, which is the default).
REM The resulting dist\MRI_instructions.exe runs on any Windows PC without Python.
cd /d "%~dp0"
if not exist .venv_build python -m venv .venv_build
call .venv_build\Scripts\activate.bat
pip install -r requirements.txt
pyinstaller --noconfirm --clean --onefile --windowed --name MRI_instructions animation.py
copy /Y config_example.json dist\config_example.json
del MRI_instructions.spec
echo.
echo Done: dist\MRI_instructions.exe
pause
