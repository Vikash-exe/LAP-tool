"""The inputs of the two kinds of search.

  Alignment (linear): an alignment file (one centreline) + a right-of-way file (two lines, or a closed polygon)
                      + the start chainage in metres and which end of the centreline it belongs to.
  Area:               an area of interest, a closed polygon.

Each file can be a KML, KMZ, GeoJSON, GeoPackage, GML, a zipped or loose shapefile, or a DXF (DXF has no
coordinate system, so the dashboard asks for one). A file is uploaded once; the preview and the search refer to
it by a token, like the DXF uploads they share a folder with (kept 6 hours).
"""

import re
import secrets
import shutil
import tempfile
from pathlib import Path

from fastapi import HTTPException

from . import alignment, aoi, dxf, query

ROLES = ("alignment", "row", "area")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def save(parts: list[tuple[str, object]]) -> dict:
    """Stores the uploaded file(s); returns what the dashboard needs: token, name, kind ("dxf" or "gis"), CRS choices."""
    if not parts:
        raise HTTPException(400, "Choose a file first.")
    names = [Path(n or "").name for n, _ in parts]
    if any(Path(n).suffix.lower() == ".dxf" for n in names):
        if len(parts) != 1:
            raise HTTPException(400, "Choose the DXF file on its own.")
        token, path = dxf.save_upload(*parts[0])
        try:
            info = dxf.inspect(path, "alignment")
        except Exception:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise
        return {"token": token, "filename": names[0], "kind": "dxf", "crs_options": info["crs_options"], "crs_in_file": info["crs_in_file"]}
    dxf._purge_old_uploads()
    token = secrets.token_hex(16)
    folder = dxf.UPLOADS_DIR / token / "gis"
    folder.mkdir(parents=True)
    try:
        aoi.save_upload_parts(parts, folder)
    except Exception:
        shutil.rmtree(folder.parent, ignore_errors=True)
        raise
    return {"token": token, "filename": ", ".join(names), "kind": "gis", "crs_options": [], "crs_in_file": None}


def _where(token: str) -> tuple[str, Path]:
    if not token or not _TOKEN.match(token):
        raise HTTPException(400, "Choose the file first.")
    base = dxf.UPLOADS_DIR / token
    if (base / "file.dxf").exists():
        return "dxf", base / "file.dxf"
    if (base / "gis").is_dir():
        return "gis", base / "gis"
    raise HTTPException(404, "The uploaded file is no longer on the server (uploads are kept 6 hours). Choose it again.")


def _walk(geom: dict | None, polygons: list[dict], lines: list[dict]) -> None:
    if not geom:
        return
    kind = geom.get("type")
    if kind in ("Polygon", "MultiPolygon"):
        polygons.append(geom)
    elif kind == "LineString":
        lines.append(geom)
    elif kind == "MultiLineString":
        lines.extend({"type": "LineString", "coordinates": part} for part in geom["coordinates"])
    elif kind == "GeometryCollection":
        for part in geom.get("geometries", []):
            _walk(part, polygons, lines)


def _fc(geoms: list[dict], role: str) -> dict:
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": g, "properties": {"role": role}} for g in geoms]}


def _is_closed(line: dict) -> bool:
    c = line["coordinates"]
    return len(c) >= 4 and c[0][0] == c[-1][0] and c[0][1] == c[-1][1]


def _read_gis(folder: Path, role: str) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory(prefix="lap_input_") as tmp:
        work = Path(tmp) / "in"
        shutil.copytree(folder, work)  # reading unpacks archives and writes converted files next to the upload
        area = aoi.load_aoi(work)
    polygons: list[dict] = []
    lines: list[dict] = []
    for f in area["features"]:
        _walk(f.get("geometry"), polygons, lines)
    closed = [{"type": "Polygon", "coordinates": [l["coordinates"]]} for l in lines if _is_closed(l)]
    open_lines = [l for l in lines if not _is_closed(l)]

    if role == "alignment":
        if not lines:
            raise HTTPException(400, "The alignment file holds no line. The alignment must be one line (the centreline).")
        return alignment.centreline(_fc(lines, role))

    if polygons or closed:
        shapes = polygons + (closed if not polygons else [])
        return _fc(shapes, role), {"kind": "polygon", "count": len(shapes), "warning": None}
    if role == "area":
        raise HTTPException(400, "The area of interest needs a closed polygon, and this file holds none.")
    if len(open_lines) >= 2:
        two, info = alignment.pick(_fc(open_lines, role))
        if info is None or len(two["features"]) != 2:
            raise HTTPException(400, "Could not find two lines for the right of way. Upload it as two lines or one closed polygon.")
        return two, {"kind": "two_lines", "lengths_m": info["lengths_m"], "warning": info.get("warning")}
    raise HTTPException(400, "The right of way needs two lines or a closed polygon, and this file holds neither.")


def _ends(fc: dict) -> dict:
    coords = fc["features"][0]["geometry"]["coordinates"]
    return {"A": coords[0][:2], "B": coords[-1][:2]}


_cache: dict[tuple, tuple[dict, dict]] = {}  # an alignment is read again for every change of the start chainage or step
CACHE_SIZE = 8


def read(token: str, role: str, crs) -> tuple[dict, dict]:
    """The geometry of an uploaded file for its role, in EPSG:4326, and a summary of what was found.

    alignment: one LineString (the centreline), summary has its length and its two ends A (first point) and B (last).
    row: two LineStrings, or polygon(s).  area: polygon(s)."""
    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    key = (token, role, str(crs))
    if key in _cache:
        return _cache[key]
    kind, where = _where(token)
    if kind == "dxf":
        if role == "alignment":
            fc, info = dxf.load_alignment(where, crs, "single")
        else:
            fc, info = dxf.load_row(where, crs, role)
    else:
        fc, info = _read_gis(where, role)
    if role == "alignment":
        info = {**info, "length_m": info["lengths_m"][0], "ends": _ends(fc)}
    if len(_cache) >= CACHE_SIZE:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (fc, info)
    return fc, info


def to_search_area(row_or_area: dict, info: dict) -> tuple[dict, list[dict] | None]:
    """(the geometry to search, the two lines to turn into a strip or None)."""
    lines = query.extract_two_lines(row_or_area) if info.get("kind") == "two_lines" else None
    return row_or_area, lines
