# Production image. Build arg PROFILE selects which profile the container runs.
#
# Image size: ~700 MB (Python slim + LibreOffice). Needs ~512MB-1GB of memory:
# LibreOffice spikes to roughly 400MB while converting DOCX to PDF. Drop the
# LibreOffice layer if you do not need the letter download — it is by far the
# largest part of the image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JOBFINDER_PROFILE=example \
    LIBREOFFICE_PATH=/usr/bin/soffice

# System deps:
#   libreoffice (headless) — DOCX→PDF rendering for the letter download
#   libxml2/libxslt — for lxml (HTML parsing in scrapers)
#   ca-certificates + curl — generic safety net
#   libreoffice-core (no java needed for our CLI use)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-common fonts-dejavu \
        libxml2 libxslt1.1 \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cache-friendly: deps change less often than code)
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir .

# Copy source, dashboard, config and assets. Only the example profile ships
# in the image; mount or copy your own profile and assets at deploy time so
# personal data never lands in a built layer.
COPY src/ ./src/
COPY dashboard/ ./dashboard/
COPY config/profile_example.yaml ./config/profile_example.yaml
COPY assets/example/ ./assets/example/

# Persistent volume mount-points (declared so Fly knows about them).
# `data/` holds the SQLite DB, CSV exports and the notification outbox.
RUN mkdir -p /app/data /app/data/exports /app/data/outbox/notifications

EXPOSE 8000

# Bind to 0.0.0.0 so the container is reachable. No --reload (production).
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
