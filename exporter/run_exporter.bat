@echo off
rem Friends of Duty content exporter launcher (Windows).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 friends_of_duty_exporter.py %*
) else (
    python friends_of_duty_exporter.py %*
)
