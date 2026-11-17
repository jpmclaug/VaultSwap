@echo off
title VaultSwap // MTG Pod Binder & Trade Feed
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_app.ps1"
if errorlevel 1 pause
