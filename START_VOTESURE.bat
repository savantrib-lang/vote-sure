@echo off
cd /d "%~dp0"
title VoteSure Biometric Voting System
cls
echo ============================================================
echo         VOTESURE - SMART FACE RECOGNITION SYSTEM
echo ============================================================
echo [1/2] Starting Flask Backend Server (Port 5000)...
start "VoteSure Server" python app.py
timeout /t 3 /nobreak >nul
echo [2/2] Starting Secure Cloudflare Tunnel...
echo Look below for your public HTTPS link (https://...trycloudflare.com)
echo Keep this window OPEN while testing or voting!
echo ============================================================
if exist "cloudflared.exe" (
  cloudflared.exe tunnel --url http://127.0.0.1:5000
) else (
  echo ERROR: cloudflared.exe not found in this folder!
  pause
)
