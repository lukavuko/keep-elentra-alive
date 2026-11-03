@echo off
REM Elentra keepalive launcher (foreground — shows live status; Ctrl+C to stop; may take up to ~25 seconds to stop).
REM Load the Brave extension in .\brave-extension once first (see its README).
setlocal
cd /d "%~dp0"
python elentra_keepalive.py %*
echo.
echo Keepalive exited.
pause
