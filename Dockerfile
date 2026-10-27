FROM python:3.12-slim

# System deps for PyMuPDF, Pillow, ODL (Java required), and Tesseract OCR
# (tesseract-ocr is the binary pytesseract shells out to; -eng is the only
# language pack needed since ocr_service.py hardcodes lang="eng"), plus
# PaddleOCR's runtime libs: libgomp1 for paddlepaddle's OpenMP CPU kernels,
# libgl1 + libglib2.0-0 for opencv-contrib-python (pulled in by paddlex).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmupdf-dev \
    mupdf-tools \
    libfreetype6 \
    libjpeg62-turbo \
    libopenjp2-7 \
    default-jre-headless \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

# Persistent volumes for uploads and SQLite DB
RUN mkdir -p uploads instance

EXPOSE 5000

ENV FLASK_APP=app.py
ENV FLASK_ENV=production
# Without this, Python block-buffers stdout/stderr whenever it isn't attached
# to a real TTY -- exactly the case under `docker logs`, since it captures a
# pipe. All the [Pipeline]/[Combined]/[Vision Extraction] progress print()
# calls this app relies on for live diagnosis can then sit unflushed for a
# while, making `docker logs --follow` lag behind what's actually happening
# and look "stuck" even when processing is proceeding normally.
ENV PYTHONUNBUFFERED=1

# docker-compose.yml's healthcheck: overrides this when running via compose;
# declared here too so a plain `docker run` (no compose) still gets a health
# probe restart: unless-stopped alone can't detect a hung/unresponsive
# process without one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=3)" || exit 1

# entrypoint.sh fails fast with a clear error if the docker-compose
# ./templates bind mount is shadowing the baked-in templates/ with an
# empty/missing directory, instead of silently serving a broken app.
CMD ["/app/entrypoint.sh"]
