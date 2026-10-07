@echo off
REM The House. Needs Python 3.10+ and Ollama (0.9 or newer) with the model pulled:  ollama pull qwen3:8b
cd /d "%~dp0"
start "" http://localhost:8765
python house.py --model qwen3:8b --agents 4 --tier 1 %*
pause
