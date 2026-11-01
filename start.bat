@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: follow the installation steps in README.md.
    pause
    exit /b 1
)
set /p "COURSE_URL=Paste the course URL and press Enter: "
if not defined COURSE_URL exit /b 1
".venv\Scripts\python.exe" watch.py --url "%COURSE_URL%" %*
pause
