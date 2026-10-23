#!/usr/bin/env bash
# Rebuilds web/frontend/dist (type-check + production build). Works without npm by calling Node directly.
set -euo pipefail
cd "$(dirname "$0")/frontend"
if command -v npm >/dev/null 2>&1; then npm run build; else
  node node_modules/typescript/bin/tsc -b
  node node_modules/vite/bin/vite.js build
fi
# maplibre-gl 6 loads its render worker as a separate file next to the app's script (assets/maplibre-gl-worker.mjs).
# Vite does not copy it into dist, and without it the map stays blank, so copy the one that belongs to the installed version.
cp node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs dist/assets/
