@echo off
rem GMMK Numpad Suite: tray icon + dashboard at http://127.0.0.1:8765/
start "" "%USERPROFILE%\.conda\envs\gmmk-obs\pythonw.exe" -I "%~dp0suite\app.py"
