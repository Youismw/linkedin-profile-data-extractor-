@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0src;%PYTHONPATH%
start "" "%~dp0.venv\Scripts\pythonw.exe" -m profile_extractor gui %*
