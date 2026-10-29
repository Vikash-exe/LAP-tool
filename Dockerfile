# The Cadastral Parcel Finder (LAP): API + built dashboard in one image.
#
#   docker compose build        # from the folder that holds this file
#
# Stage 1 builds the dashboard (Vite / React / MapLibre). Stage 2 is the app: Python 3.13 with GDAL
# (ogr2ogr / ogrinfo read the uploaded areas and the DXF files), the backend and the built dashboard.
# It must run as ONE process (no extra uvicorn workers): search jobs and state locks live in its memory.

# ---- stage 1: the dashboard ------------------------------------------------------------
FROM node:20-bookworm-slim AS frontend
WORKDIR /src/web
COPY web/frontend/package.json web/frontend/package-lock.json frontend/
RUN cd frontend && npm ci
COPY web/frontend frontend
COPY web/build_frontend.sh .
# type-check + production build, and copy MapLibre's worker file next to it (see build_frontend.sh)
RUN chmod +x build_frontend.sh && ./build_frontend.sh

# ---- stage 2: the app ------------------------------------------------------------------
FROM python:3.13-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends gdal-bin ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Same numeric user and group as the host user that owns the project, so the mounted folders
# (results, the parcel data share, ~/.pgpass) can be read and written with the right ownership.
RUN groupadd -g 1002 lap && useradd -u 1001 -g 1002 -m -s /bin/bash lap

WORKDIR /app
COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY web/backend web/backend
COPY --from=frontend /src/web/frontend/dist web/frontend/dist
COPY src src
# web/data holds search results and uploaded DXFs; data/mount is where the parcel data share is mounted
RUN mkdir -p web/data data/mount && chown -R lap:lap /app

ENV PYTHONUNBUFFERED=1 \
    HOME=/home/lap \
    PGPASSFILE=/home/lap/.pgpass \
    LAP_AUTH_FILE=/app/auth/users
USER lap
WORKDIR /app/web
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=8).read()"

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--app-dir", ".", "--host", "0.0.0.0", "--port", "8010"]
