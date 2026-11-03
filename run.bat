@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0src;%PYTHONPATH%
"%~dp0.venv\Scripts\python.exe" -m profile_extractor %*

