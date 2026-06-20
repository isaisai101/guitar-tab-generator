@echo off
echo Starting Guitar Tab Generator...
call venv\Scripts\activate.bat
start "" http://localhost:5000
python backend\app.py
