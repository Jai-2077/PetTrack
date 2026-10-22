@echo off
cd /d "%~dp0"
if not exist .venv ( py -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
rem set PETTRACK_PORT=COM8
rem set PETTRACK_CLOUD_URL=https://your-app.onrender.com
rem set PETTRACK_SYNC_KEY=long-random-string
python run.py
pause
