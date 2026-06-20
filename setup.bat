@echo off
echo ============================================================
echo  Guitar Tab Generator - Setup (Windows + CUDA)
echo ============================================================

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    pause & exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo WARNING: ffmpeg not found.
    echo   yt-dlp requires ffmpeg for audio extraction.
    echo   Install via: winget install ffmpeg
    echo   Or download from: https://ffmpeg.org/download.html
    echo.
)

echo Creating virtual environment...
python -m venv venv
call venv\Scripts\activate.bat

echo.
echo Installing PyTorch with CUDA 12.1 support...
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

echo.
echo Installing remaining dependencies...
pip install -r backend\requirements.txt

echo.
echo ============================================================
echo  Setup complete!
echo  Run:  start.bat
echo ============================================================
pause
