#!/usr/bin/env bash
# Frontend dev server on 127.0.0.1:5174 (proxies /api to the backend on 8010). Only needed while changing the
# frontend: the backend already serves the built version. Works without npm by calling Node directly.
cd "$(dirname "$0")/frontend"
if command -v npm >/dev/null 2>&1; then exec npm run dev; fi
exec node node_modules/vite/bin/vite.js
