#!/usr/bin/env bash
# Starts the dashboard: the API and, if web/frontend/dist exists, the built frontend, on one port.
# HOST (default 127.0.0.1) and PORT (default 8010) can be set; pass --reload while developing.
# The Python environment is ../lapenv (made by ../setup.sh); set LAP_PYTHON to use another.
cd "$(dirname "$0")"
PY="${LAP_PYTHON:-../lapenv/bin/python}"
exec "$PY" -m uvicorn backend.app.main:app --app-dir . --host "${HOST:-127.0.0.1}" --port "${PORT:-8010}" "$@"
