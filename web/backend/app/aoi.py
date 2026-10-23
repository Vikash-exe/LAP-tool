"""Reads an uploaded area-of-interest file (KML, KMZ, zipped or loose
Shapefile, GeoJSON, GeoPackage) into a GeoJSON FeatureCollection in EPSG:4326.

Conversion goes through the GDAL command-line tools (ogrinfo/ogr2ogr), which
already understand every format listed above, including multi-layer KML.
"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from fastapi import HTTPException

from . import config

GDAL_TIMEOUT_S = 120


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GDAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise HTTPException(400, "Reading the uploaded file took too long.")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise HTTPException(400, f"Could not read the uploaded file: {detail[-1] if detail else 'unknown error'}")
    return proc.stdout


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extracts a zip/kmz, refusing members that would land outside dest."""
    dest = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = (dest / member.filename).resolve()
                if dest != target and dest not in target.parents:
                    raise HTTPException(400, "Archive contains an unsafe path.")
            total = sum(m.file_size for m in zf.infolist())
            if total > config.MAX_UPLOAD_MB * 1024 * 1024 * 5:
                raise HTTPException(400, "Archive expands to an unreasonable size.")
            zf.extractall(dest)
    except zipfile.BadZipFile:
        raise HTTPException(400, f"{archive.name} is not a valid zip/kmz archive.")


def _find_sources(directory: Path) -> list[Path]:
    found = [
        p
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.suffix.lower() in config.AOI_SUFFIXES and not p.name.startswith(".")
    ]
    # A KMZ's doc.kml and a shapefile's own files can both show up; shapefiles
    # win over nothing else here, every primary file is read.
    return found


def _count_vertices(node) -> int:
    if node and isinstance(node[0], (int, float)):
        return 1
    return sum(_count_vertices(child) for child in node)


def _layers_with_geometry(src: Path) -> list[str]:
    info = json.loads(_run(["ogrinfo", "-json", "-so", str(src)]))
    names = []
    for layer in info.get("layers", []):
        if layer.get("geometryFields") and layer.get("featureCount", 1) != 0:
            names.append(layer["name"])
    return names


def load_aoi(workdir: Path) -> dict:
    """workdir holds the raw uploaded file(s). Returns a 4326 FeatureCollection."""
    # Unpack archives first so their contents are discoverable.
    for archive in [p for p in workdir.iterdir() if p.suffix.lower() in config.ARCHIVE_SUFFIXES]:
        extracted = workdir / (archive.stem + "_unpacked")
        extracted.mkdir(exist_ok=True)
        _safe_extract(archive, extracted)

    sources = _find_sources(workdir)
    if not sources:
        raise HTTPException(
            400,
            "No supported file found. Upload a .kml, .kmz, .geojson, .gpkg, a zipped shapefile (.zip), "
            "or the loose shapefile parts (.shp + .dbf + .shx + .prj).",
        )

    features: list[dict] = []
    for i, src in enumerate(sources):
        layers = _layers_with_geometry(src)
        for j, layer in enumerate(layers):
            out = workdir / f"converted_{i}_{j}.geojson"
            cmd = ["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-dim", "XY", "-skipfailures"]
            # A shapefile with no .prj has no CRS to transform from; assume
            # lon/lat and let the bounds check below reject projected coordinates.
            if src.suffix.lower() == ".shp" and not src.with_suffix(".prj").exists():
                cmd += ["-a_srs", "EPSG:4326"]
            cmd += [str(out), str(src), layer]
            _run(cmd)
            fc = json.loads(out.read_text(encoding="utf-8"))
            for feat in fc.get("features", []):
                geom = feat.get("geometry")
                if geom:
                    features.append({"type": "Feature", "geometry": geom, "properties": {}})

    if not features:
        raise HTTPException(400, "The uploaded file contains no geometries.")

    vertices = sum(_count_vertices(f["geometry"].get("coordinates", [])) for f in features if f["geometry"].get("coordinates"))
    if vertices > config.MAX_AOI_VERTICES:
        raise HTTPException(400, f"The area has {vertices:,} vertices (limit {config.MAX_AOI_VERTICES:,}). Simplify it first.")

    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    def visit(node):
        nonlocal minx, miny, maxx, maxy
        if node and isinstance(node[0], (int, float)):
            x, y = node[0], node[1]
            minx, maxx = min(minx, x), max(maxx, x)
            miny, maxy = min(miny, y), max(maxy, y)
        else:
            for child in node:
                visit(child)

    for f in features:
        visit(f["geometry"].get("coordinates", []))
    if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
        raise HTTPException(
            400,
            "Coordinates are outside lon/lat range: the file looks projected but has no coordinate system "
            "defined (e.g. a shapefile without its .prj). Add the .prj or reproject to EPSG:4326.",
        )

    return {"type": "FeatureCollection", "features": features}


def save_upload_parts(parts: list[tuple[str, "object"]], workdir: Path) -> None:
    """Writes uploaded (filename, file-like) pairs into workdir, enforcing the size cap."""
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    allowed = config.AOI_SUFFIXES + config.ARCHIVE_SUFFIXES + config.SIDECAR_SUFFIXES
    for filename, fileobj in parts:
        name = Path(filename or "").name
        if not name or Path(name).suffix.lower() not in allowed:
            raise HTTPException(400, f"Unsupported file type: {name or '(unnamed)'}")
        dest = workdir / name
        with open(dest, "wb") as out:
            while chunk := fileobj.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    shutil.rmtree(workdir, ignore_errors=True)
                    raise HTTPException(413, f"Upload exceeds {config.MAX_UPLOAD_MB} MB.")
                out.write(chunk)
