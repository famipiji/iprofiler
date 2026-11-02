@echo off
REM Starts iProfiler locally (Flask on http://localhost:5000).
cd /d "%~dp0"
title iProfiler
REM MySQL (dms_iprofiler) and LM Studio both live on the X2 server.
REM app.py defaults to the Docker host name otherwise.
if not defined DB_HOST set DB_HOST=192.168.1.18
echo Starting iProfiler on http://localhost:5000 ...
REM Show output here AND save it to iprofiler.log (for troubleshooting).
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
powershell -NoProfile -Command "python app.py 2>&1 | ForEach-Object { \"$_\" } | Tee-Object -FilePath iprofiler.log"
echo.
echo iProfiler stopped (exit code %ERRORLEVEL%). Press any key to close.
pause >nul
