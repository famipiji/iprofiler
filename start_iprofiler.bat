@echo off
REM Starts iProfiler locally (Flask on http://localhost:5000).
cd /d "%~dp0"
title iProfiler
REM MySQL (dms_iprofiler) and LM Studio both live on the X2 server.
REM app.py defaults to the Docker host name otherwise.
if not defined DB_HOST set DB_HOST=192.168.1.18
if not defined OLLAMA_BASE_URL set OLLAMA_BASE_URL=http://192.168.1.18:1234/v1
REM Thinking must be OFF for this model in LM Studio, or answers come back empty.
if not defined OLLAMA_MODEL set OLLAMA_MODEL=qwen/qwen3.5-9b
echo Starting iProfiler on http://localhost:5000 ...
REM Show output here AND save it to iprofiler.log (for troubleshooting).
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
powershell -NoProfile -Command "python app.py 2>&1 | ForEach-Object { \"$_\" } | Tee-Object -FilePath iprofiler.log"
echo.
echo iProfiler stopped (exit code %ERRORLEVEL%). Press any key to close.
pause >nul
