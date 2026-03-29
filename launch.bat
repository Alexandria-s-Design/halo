@echo off
title Halo Voice Companion
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python halo.py --debug
pause
