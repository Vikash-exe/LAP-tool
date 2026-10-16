#!/usr/bin/env bash
# Starts the UI dev server on 127.0.0.1:5174 (proxies /api to the backend on 8010).
cd "$(dirname "$0")/frontend"
exec npm run dev
