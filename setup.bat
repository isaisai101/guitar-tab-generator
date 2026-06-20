@echo off
echo ============================================================
echo  Guitar Tab Generator - Setup (Windows + CUDA / RTX 5000+)
echo ============================================================

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo WARNING: ffmpeg not in PATH yet. You may need to restart this terminal.
    echo   If yt-dlp fails, run:  winget install Gyan.FFmpeg
    echo.
)

echo Using Python 3.12 (required for PyTorch compatibility)...
py -3.12 --version
if errorlevel 1 (
    echo ERROR: Python 3.12 not found.
    echo   Install via:  winget install Python.Python.3.12
    pause & exit /b 1
)

echo.
echo Creating virtual environment with Python 3.12...
py -3.12 -m venv venv
call venv\Scripts\activate.bat

echo.
echo Installing PyTorch with CUDA 12.8 support (RTX 5060 / Blackwell)...
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

echo.
echo Installing remaining dependencies...
pip install -r backend\requirements.txt

echo.
echo ============================================================
echo  Setup complete!
echo  Run:  start.bat
echo ============================================================
pause
