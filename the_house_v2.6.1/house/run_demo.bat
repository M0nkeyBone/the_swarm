@echo off
REM Try the viewer with no model at all (a stand-in fake model plays the agents).
cd /d "%~dp0"
start "" http://localhost:8765
python house.py --fake --db demo.db %*
pause
