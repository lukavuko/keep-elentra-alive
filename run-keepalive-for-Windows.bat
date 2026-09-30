@echo off
REM Elentra keepalive launcher (foreground — shows live status; Ctrl+C to stop; may take up to ~25 seconds to stop).
REM Load the Brave extension in .\brave-extension once first (see its README).
setlocal
cd /d "%~dp0"
echo Starting Elentra keepalive...
echo Listening for the Brave/Chrome extension's cookie pushes on 127.0.0.1:8765
echo Log in to Elentra in Brave/Chrome; watch for "Logged in to Elentra". Ctrl+C to stop (may take up to ~25 seconds to stop).
echo.
python elentra_keepalive.py %*
echo.
echo Keepalive exited.
pause
