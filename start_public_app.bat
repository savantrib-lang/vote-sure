@echo off
cd /d "%~dp0"
title VoteSure Public Internet Server
echo Starting VoteSure Backend...
start "VoteSure Server" python app.py
timeout /t 3 /nobreak >nul
echo Starting Public Cloudflare Tunnel...
if exist "cloudflared.exe" (
  cloudflared.exe tunnel --url http://127.0.0.1:5000
) else (
  echo cloudflared.exe not found!
  pause
)
