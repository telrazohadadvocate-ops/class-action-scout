@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONLEGACYWINDOWSSTDIO=1
cd /d C:\Projects\class-action-scout
call venv\Scripts\activate.bat
python -X utf8 -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); sys.stderr.reconfigure(encoding='utf-8', errors='replace'); exec(open('main.py', encoding='utf-8').read())" %*