@echo off
rem Builds dist\G2 Switcher.exe (single file, no console window).
rem Needs Python 3.10+ on PATH:  pip install -r requirements.txt pyinstaller
cd /d "%~dp0"
python theme.py || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "G2 Switcher" --icon g2switcher.ico ^
  --hidden-import pystray._win32 ^
  app.py || exit /b 1
echo.
echo Built: dist\G2 Switcher.exe
echo Run install.ps1 to add Start menu / desktop shortcuts.
