FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    READCUE_DATA_DIR=/data

# tzdata: so the TZ variable works (reminders fire at a local time of day)
# tesseract-ocr + poppler-utils: OCR for scanned PDFs and photos of pages
# EXTRA_OCR_PACKAGES: more OCR languages, e.g. "tesseract-ocr-spa tesseract-ocr-fra"
ARG EXTRA_OCR_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata tesseract-ocr tesseract-ocr-eng poppler-utils $EXTRA_OCR_PACKAGES \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 readcue \
    && mkdir /data \
    && chown readcue:readcue /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

ARG VERSION=dev
LABEL org.opencontainers.image.title="readcue" \
      org.opencontainers.image.description="Summarize textbook chapters ahead of their syllabus due dates and remind you via Pushover." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

USER readcue
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

# The address the port is published on is READCUE_BIND (docker-compose.yml sets it). Without a password the app
# refuses to start if that is anything but localhost.
ENTRYPOINT ["readcue"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
