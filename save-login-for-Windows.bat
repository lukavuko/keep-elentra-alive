@echo off
REM Saves (or replaces) your CWL login in Windows Credential Manager for background re-login.
REM Run "python elentra_keepalive.py --forget-login" to remove it.
setlocal
cd /d "%~dp0"
python elentra_keepalive.py --set-login
echo.
pause
