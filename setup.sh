#!/usr/bin/env bash
# One-time setup of the Python environment, with the exact package versions the project was built and tested with.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv lapenv
lapenv/bin/pip install --quiet -r requirements-lock.txt
mkdir -p data/mount web/data/results
echo "Python environment ready: $(lapenv/bin/python --version), $(lapenv/bin/pip list 2>/dev/null | wc -l) packages"
for tool in ogr2ogr ogrinfo; do
  command -v "$tool" >/dev/null 2>&1 || echo "WARNING: $tool (GDAL command-line tools) not found: reading uploaded KML/shapefiles and exports need it."
done
[ -f "$HOME/.pgpass" ] || echo "NOTE: ~/.pgpass not found: create it with the database password (see web/README.md, 'Setting up on a new machine')."
echo "Next: put (or mount) the parcel data under data/mount, then run  web/run_backend.sh"
