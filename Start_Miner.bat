@echo off
cd /d "%~dp0"
start /affinity 7FFF /b python PIC_Miner.py
pause
