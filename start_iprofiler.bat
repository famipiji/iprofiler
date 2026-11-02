@echo off
REM Starts iProfiler locally (Flask on http://localhost:5000).
cd /d "%~dp0"
title iProfiler
REM MySQL (dms_iprofiler) and LM Studio both live on the X2 server.
REM app.py defaults to the Docker host name otherwise.
if not defined DB_HOST set DB_HOST=192.168.1.18
REM Portable Java (Eclipse Temurin 17) in .\jre - needed by opendataloader-pdf for digital PDFs.
if exist "%~dp0jre\bin\java.exe" set "PATH=%~dp0jre\bin;%PATH%"
if not defined OLLAMA_BASE_URL set OLLAMA_BASE_URL=http://192.168.1.18:1234/v1
REM Must be a NON-thinking Instruct vision model (thinking models return empty answers).
if not defined OLLAMA_MODEL set OLLAMA_MODEL=qwen3-vl-8b-instruct
REM Scanned PDFs/images: vision = AI reads the page (default), paddle = PaddleOCR reads it (faster).
if not defined OCR_TEXT_SOURCE set OCR_TEXT_SOURCE=paddle
echo Starting iProfiler on http://localhost:5000 ...
REM Show output here AND save it to iprofiler.log (for troubleshooting).
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
powershell -NoProfile -Command "python app.py 2>&1 | ForEach-Object { \"$_\" } | Tee-Object -FilePath iprofiler.log"
echo.
echo iProfiler stopped (exit code %ERRORLEVEL%). Press any key to close.
pause >nul
