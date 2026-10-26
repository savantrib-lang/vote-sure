@echo off
cd /d "%~dp0"
title Smart Voting System
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe app.py
) else (
  python app.py
)
pause
