#!/usr/bin/env bash
# Starts the API on 127.0.0.1:8010. Pass --reload while developing.
cd "$(dirname "$0")"
exec ../lapenv/bin/python -m uvicorn backend.app.main:app --app-dir . --host 127.0.0.1 --port 8010 "$@"
