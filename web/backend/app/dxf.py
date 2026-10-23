"""DXF inputs: the alignment (one or two long lines) and the chainage labels (text points).

DXF files carry no coordinate system, so the caller always supplies an EPSG code (the dashboard offers a
list, pre-selecting the ones that put the drawing on Indian land). Everything is read with GDAL
(ogr2ogr / ogrinfo, SQLite dialect), which also reprojects to EPSG:4326.

Arcs are NOT straightened: GDAL cuts arcs and polyline bulges into short segments, and its default of 4
degrees per segment shortens a curve measurably, so OGR_ARC_STEPSIZE is set to 0.05 degrees (the length of
77 km of curves converges to within 0.2 m of the exact value, instead of being 9 m short).

Uploads are kept for a few hours under a token, so inspect / preview / search do not each re-send a file
that can be tens of MB.
"""

import csv
import io
import json
import math
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import config, query

UPLOADS_DIR = config.WEB_ROOT / "data" / "dxf_uploads"
UPLOAD_KEEP_SECONDS = 6 * 3600
ARC_STEP = "0.05"  # degrees per arc segment
GDAL_TIMEOUT_S = 600
LAYER = "entities"  # the one layer GDAL's DXF driver exposes

# EPSG codes offered in the CRS drop-down (names as PROJ reports them). PostGIS knows all of them.
CRS_CHOICES = [
    (4326, "WGS 84 (longitude / latitude)"),
    (32642, "WGS 84 / UTM zone 42N"),
    (32643, "WGS 84 / UTM zone 43N"),
    (32644, "WGS 84 / UTM zone 44N"),
    (32645, "WGS 84 / UTM zone 45N"),
    (32646, "WGS 84 / UTM zone 46N"),
    (7755, "WGS 84 / India NSF LCC"),
    (24342, "Kalianpur 1975 / UTM zone 42N"),
    (24343, "Kalianpur 1975 / UTM zone 43N"),
    (24344, "Kalianpur 1975 / UTM zone 44N"),
    (24345, "Kalianpur 1975 / UTM zone 45N"),
    (24346, "Kalianpur 1975 / UTM zone 46N"),
    (24347, "Kalianpur 1975 / UTM zone 47N"),
    (24378, "Kalianpur 1975 / India zone I"),
    (24379, "Kalianpur 1975 / India zone IIa"),
    (24380, "Kalianpur 1975 / India zone IIb"),
    (24381, "Kalianpur 1975 / India zone IIIa"),
    (24383, "Kalianpur 1975 / India zone IVa"),
    (3857, "WGS 84 / Pseudo-Mercator (Web Mercator)"),
]

_TOKEN = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------- GDAL helpers

def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GDAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise HTTPException(400, "Reading the DXF file took too long.")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise HTTPException(400, f"Could not read the DXF file: {detail[-1] if detail else 'unknown error'}")
    return proc.stdout


def _ogr2ogr(path: Path, sql_text: str, fmt: str, extra: list[str] | None = None) -> str:
    return _run(
        ["ogr2ogr", "--config", "OGR_ARC_STEPSIZE", ARC_STEP, "-f", fmt, "/vsistdout/", str(path),
         "-dialect", "sqlite", "-sql", sql_text, *(extra or [])]
    )


def _rows(path: Path, sql_text: str) -> list[dict]:
    """The result of a SQL query over the DXF's entities as a list of dicts (geometry is not included)."""
    return list(csv.DictReader(io.StringIO(_ogr2ogr(path, sql_text, "CSV"))))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _check_crs(crs) -> int:
    try:
        code = int(crs)
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose the coordinate system (CRS) of the DXF file.")
    if code <= 0:
        raise HTTPException(400, "Choose the coordinate system (CRS) of the DXF file.")
    try:
        with query.connect() as conn:
            known = conn.execute("SELECT 1 FROM spatial_ref_sys WHERE srid = %s", (code,)).fetchone()
    except psycopg.Error:
        known = True  # cannot check; GDAL will complain if it is wrong
    if not known:
        raise HTTPException(400, f"EPSG:{code} is not a known coordinate system.")
    return code


# ---------------------------------------------------------------- uploads

def _purge_old_uploads() -> None:
    if not UPLOADS_DIR.exists():
        return
    cutoff = time.time() - UPLOAD_KEEP_SECONDS
    for d in UPLOADS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def save_upload(filename: str | None, fileobj) -> tuple[str, Path]:
    """Stores an uploaded .dxf and returns (token, path)."""
    name = Path(filename or "").name
    if Path(name).suffix.lower() != ".dxf":
        raise HTTPException(400, "Choose a .dxf file.")
    _purge_old_uploads()
    token = secrets.token_hex(16)
    folder = UPLOADS_DIR / token
    folder.mkdir(parents=True)
    path = folder / "file.dxf"
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with open(path, "wb") as out:
        while chunk := fileobj.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(413, f"Upload exceeds {config.MAX_UPLOAD_MB} MB.")
            out.write(chunk)
    (folder / "name.txt").write_text(name, encoding="utf-8")
    return token, path


def path_for(token: str) -> Path:
    if not token or not _TOKEN.match(token):
        raise HTTPException(400, "Choose the DXF file first.")
    path = UPLOADS_DIR / token / "file.dxf"
    if not path.exists():
        raise HTTPException(404, "The uploaded DXF is no longer on the server (uploads are kept 6 hours). Choose the file again.")
    return path


# ---------------------------------------------------------------- inspect

def _fields(path: Path) -> tuple[list[dict], dict | None]:
    info = json.loads(_run(["ogrinfo", "-ro", "-json", "-so", str(path), LAYER]))
    layer = info["layers"][0]
    fields = layer.get("fields", [])
    crs = None
    for g in layer.get("geometryFields", []):
        wkt = (g.get("coordinateSystem") or {}).get("wkt")
        if wkt:
            ident = re.findall(r'ID\["EPSG",(\d+)\]', wkt)
            name = re.match(r'\w+\["([^"]+)"', wkt)
            crs = {"name": name.group(1) if name else "defined in the file", "code": int(ident[-1]) if ident else None}
    return fields, crs


def _crs_options(x: float | None, y: float | None, in_file: dict | None) -> list[dict]:
    """The CRS list for the drop-down. The ones that put the point (x, y) on land in a state are marked
    with that state's name and listed first; a CRS defined inside the file comes before everything."""
    hints: dict[int, str] = {}
    if x is not None and y is not None:
        lookup = sql.SQL(
            "SELECT s.name FROM {}.{} s WHERE ST_Intersects(s.geometry, "
            "ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), %s), 4326)) LIMIT 1"
        ).format(sql.Identifier(config.SCHEMA), sql.Identifier(config.STATE_BOUNDARIES))
        try:
            with query.connect() as conn:
                for code, _name in CRS_CHOICES:
                    try:
                        with conn.transaction():
                            row = conn.execute(lookup, (x, y, code)).fetchone()
                        if row:
                            hints[code] = row[0]
                    except psycopg.Error:
                        continue  # this CRS cannot hold these coordinates
        except psycopg.Error:
            pass
    options = [{"code": c, "name": n, "hint": hints.get(c)} for c, n in CRS_CHOICES]
    options.sort(key=lambda o: o["hint"] is None)  # stable: the suggested ones first
    if in_file and in_file.get("code"):
        options = [o for o in options if o["code"] != in_file["code"]]
        options.insert(0, {"code": in_file["code"], "name": in_file["name"], "hint": "defined in the file"})
    return options


def inspect(path: Path, role: str) -> dict:
    """What the dashboard needs to show the form: columns with their fill counts, the first 10 text rows,
    what the file holds, and the CRS choices. role is "alignment" or "chainage"."""
    fields, in_file = _fields(path)
    strings = [f["name"] for f in fields if f.get("type") == "String"]
    out: dict = {"fields": [], "default_field": None, "sample": {"columns": [], "rows": []}, "geometry_counts": {}}

    counts = _rows(path, f"SELECT GeometryType(geometry) AS gt, COUNT(*) AS n FROM {LAYER} GROUP BY gt")
    out["geometry_counts"] = {r["gt"]: int(r["n"]) for r in counts}
    n_points = sum(n for gt, n in out["geometry_counts"].items() if gt.upper().startswith("POINT"))

    if strings and n_points:
        exprs = []
        for i, name in enumerate(strings):
            q = _q(name)
            exprs.append(f"SUM(CASE WHEN TRIM(COALESCE({q}, '')) <> '' THEN 1 ELSE 0 END) AS e{i}")
            exprs.append(f"SUM(CASE WHEN UPPER(TRIM(COALESCE({q}, ''))) LIKE 'CH%' OR UPPER(TRIM(COALESCE({q}, ''))) LIKE 'KM%' THEN 1 ELSE 0 END) AS c{i}")
        stats = _rows(path, f"SELECT {', '.join(exprs)} FROM {LAYER} WHERE GeometryType(geometry) LIKE 'POINT%'")[0]
        for i, name in enumerate(strings):
            out["fields"].append({"name": name, "non_empty": int(stats[f"e{i}"] or 0), "chainage_like": int(stats[f"c{i}"] or 0)})
        best = max(out["fields"], key=lambda f: (f["chainage_like"], f["name"] == "Text", f["non_empty"]))
        out["default_field"] = best["name"]

        cols = ", ".join(_q(n) for n in strings)
        rows = _rows(path, f"SELECT {cols}, ST_X(geometry) AS x, ST_Y(geometry) AS y FROM {LAYER} WHERE GeometryType(geometry) LIKE 'POINT%' LIMIT 10")
        out["sample"] = {"columns": list(rows[0].keys()) if rows else [], "rows": [list(r.values()) for r in rows]}

    # One coordinate that stands for the drawing, to suggest CRSs.
    x = y = None
    try:
        if role == "alignment":
            r = _rows(path, f"SELECT ST_X(StartPoint(geometry)) AS x, ST_Y(StartPoint(geometry)) AS y FROM {LAYER} WHERE GeometryType(geometry) LIKE 'LINESTRING%' ORDER BY ST_Length(geometry) DESC LIMIT 1")
        else:
            cond = "1=1"
            if out["default_field"]:
                q = _q(out["default_field"])
                cond = f"(UPPER(TRIM(COALESCE({q}, ''))) LIKE 'CH%' OR UPPER(TRIM(COALESCE({q}, ''))) LIKE 'KM%')"
            r = _rows(path, f"SELECT ST_X(geometry) AS x, ST_Y(geometry) AS y FROM {LAYER} WHERE GeometryType(geometry) LIKE 'POINT%' AND {cond} LIMIT 1")
        if r:
            x, y = float(r[0]["x"]), float(r[0]["y"])
    except (HTTPException, ValueError, KeyError):
        pass
    out["coordinate"] = [x, y] if x is not None else None
    out["crs_in_file"] = in_file
    out["crs_options"] = _crs_options(x, y, in_file)
    return out


# ---------------------------------------------------------------- geometry helpers

def _haversine_m(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def _line_length_m(coords) -> float:
    return sum(_haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def _check_lonlat(points, crs: int, what: str) -> None:
    if not points:
        return
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not (-180 <= min(xs) and max(xs) <= 180 and -90 <= min(ys) and max(ys) <= 90):
        raise HTTPException(400, f"With EPSG:{crs} the {what} would lie outside the world. Choose another coordinate system.")


def _transform(features: dict, crs: int) -> dict:
    """A GeoJSON FeatureCollection in the drawing's CRS -> EPSG:4326 (via ogr2ogr, which handles every datum)."""
    with tempfile.TemporaryDirectory(prefix="lap_dxf_") as tmp:
        src = Path(tmp) / "in.geojson"
        src.write_text(json.dumps(features), encoding="utf-8")
        out = _run(["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", str(src), "-s_srs", f"EPSG:{crs}", "-t_srs", "EPSG:4326", "-dim", "XY"])
    return json.loads(out)


# ---------------------------------------------------------------- alignment

def load_alignment(path: Path, crs, mode: str = "auto") -> tuple[dict, dict]:
    """The alignment from a DXF: its longest line(s); every shorter line is treated as a marking and ignored.

    mode: "two" = the two longest lines are the corridor's edges, "single" = the longest line is a centreline,
    "auto" = two when the second longest is at least half as long as the longest, else one.
    Returns (FeatureCollection in EPSG:4326 holding the kept LineStrings, info for the dashboard)."""
    crs = _check_crs(crs)
    if mode not in ("auto", "two", "single"):
        raise HTTPException(400, "Alignment type must be auto, two or single.")
    total = int(_rows(path, f"SELECT COUNT(*) AS n FROM {LAYER} WHERE GeometryType(geometry) LIKE 'LINESTRING%'")[0]["n"])
    if total == 0:
        raise HTTPException(400, "The DXF holds no lines or polylines, so there is no alignment in it.")
    top = json.loads(
        _ogr2ogr(
            path,
            f"SELECT geometry, Layer AS layer, ST_Length(geometry) AS len FROM {LAYER} WHERE GeometryType(geometry) LIKE 'LINESTRING%' ORDER BY len DESC LIMIT 6",
            "GeoJSON", ["-dim", "XY"],
        )
    )["features"]
    lens = [float(f["properties"]["len"]) for f in top]
    if mode == "two":
        n = 2
    elif mode == "single":
        n = 1
    else:
        n = 2 if len(lens) >= 2 and lens[1] >= 0.5 * lens[0] else 1
    if len(top) < n:
        raise HTTPException(400, f"Only {len(top)} line(s) in the DXF: two are needed for 'two lines'.")
    kept = top[:n]
    fc = _transform({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": f["geometry"]} for f in kept]}, crs)
    pts = [c for f in fc["features"] for c in f["geometry"]["coordinates"]]
    _check_lonlat(pts, crs, "alignment")
    for f, src in zip(fc["features"], kept):
        f["properties"] = {"layer": src["properties"].get("layer"), "role": "alignment"}
    lengths_m = [round(_line_length_m(f["geometry"]["coordinates"]), 1) for f in fc["features"]]
    scale = lengths_m[0] / lens[0] if lens[0] else 0.0  # metres per drawing unit
    info = {
        "lines_found": total,
        "lines_used": n,
        "mode_used": "two" if n == 2 else "single",
        "lengths_m": lengths_m,
        "ignored": total - n,
        "ignored_longest_m": round(lens[n] * scale, 1) if len(lens) > n else None,
        "warning": None,
    }
    if len(lens) > n and lens[n] >= 0.5 * lens[n - 1]:
        info["warning"] = (
            f"The next longest line ({info['ignored_longest_m']:,.0f} m) is almost as long as the ones used. "
            "If the alignment is not what you expect, remove the extra long lines from the DXF."
        )
    return fc, info


# ---------------------------------------------------------------- chainage

_PREFIX = re.compile(r"^\s*(CH|KM)", re.IGNORECASE)
_PLUS = re.compile(r"([+-]?)\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)")
_PLAIN = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(km|m)?\b", re.IGNORECASE)
_MTEXT_CODES = re.compile(r"\\[A-Za-z][^;\\]*;|\\[PpNn~]|[{}]")


def clean_text(text: str) -> str:
    """MTEXT may carry formatting codes (\\A1; \\P ...); GDAL usually strips them, this catches the rest."""
    return _MTEXT_CODES.sub(" ", text or "").strip()


def parse_chainage(text: str) -> float | None:
    """Chainage in metres from a label that starts with CH or KM; None when it cannot be read.

    "CH -2+400" -> -2400, "KM: 12+500" -> 12500, "CH 12+345.5" -> 12345.5, "KM 12.5" -> 12500 (km),
    "CH 12500" -> 12500 (metres)."""
    m = _PREFIX.match(text)
    if not m:
        return None
    rest = text[m.end():]
    p = _PLUS.search(rest)
    if p:
        value = int(p.group(2)) * 1000 + float(p.group(3))
        return -value if p.group(1) == "-" else value
    n = _PLAIN.search(rest)
    if n:
        value = float(n.group(1))
        unit = (n.group(2) or "").lower()
        if unit == "km" or (unit == "" and m.group(1).upper() == "KM"):
            value *= 1000
        return value
    return None


def format_chainage(value: float) -> str:
    """-2400 -> "CH -2+400", 12345.4 -> "CH 12+345". (The CH prefix also stops a spreadsheet from reading it as a formula.)"""
    rounded = int(round(abs(value)))
    km, m = divmod(rounded, 1000)
    sign = "-" if value < 0 and rounded else ""
    return f"CH {sign}{km}+{m:03d}"


def read_chainage(path: Path, crs, field: str) -> tuple[list[dict], dict]:
    """The chainage labels: text points whose value in `field` starts with CH or KM.

    Returns ([{text, value, lon, lat}], info). A label that starts with CH/KM but holds no readable chainage is
    counted in info["unparsed"]."""
    crs = _check_crs(crs)
    fields, _ = _fields(path)
    names = {f["name"] for f in fields if f.get("type") == "String"}
    if field not in names:
        raise HTTPException(400, f"The DXF has no text column named '{field}'. Columns: {', '.join(sorted(names)) or 'none'}.")
    q = _q(field)
    cond = f"(UPPER(TRIM(COALESCE({q}, ''))) LIKE 'CH%' OR UPPER(TRIM(COALESCE({q}, ''))) LIKE 'KM%')"
    gj = json.loads(
        _ogr2ogr(
            path,
            f"SELECT {q} AS txt, geometry FROM {LAYER} WHERE GeometryType(geometry) LIKE 'POINT%' AND {cond}",
            "GeoJSON", ["-s_srs", f"EPSG:{crs}", "-t_srs", "EPSG:4326", "-dim", "XY"],
        )
    )
    labels: list[dict] = []
    unparsed: list[str] = []
    for f in gj["features"]:
        text = clean_text(f["properties"].get("txt") or "")
        value = parse_chainage(text)
        if value is None:
            unparsed.append(text)
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        labels.append({"text": text, "value": value, "lon": lon, "lat": lat})
    if not labels:
        counts = _rows(path, f"SELECT SUM(CASE WHEN TRIM(COALESCE({q}, '')) <> '' THEN 1 ELSE 0 END) AS e FROM {LAYER} WHERE GeometryType(geometry) LIKE 'POINT%'")
        non_empty = int(float(counts[0]["e"] or 0)) if counts else 0
        if non_empty == 0:
            raise HTTPException(400, f"The column '{field}' is empty: it holds no text. Choose another column.")
        raise HTTPException(
            400,
            f"No value in the column '{field}' starts with CH or KM"
            + (f" with a readable chainage (examples of those that start with CH/KM: {', '.join(unparsed[:3])})" if unparsed else "")
            + ". Choose another column.",
        )
    _check_lonlat([(l["lon"], l["lat"]) for l in labels], crs, "chainage labels")
    info = {
        "count": len(labels),
        "unparsed": len(unparsed),
        "unparsed_examples": unparsed[:5],
        "min": min(l["value"] for l in labels),
        "max": max(l["value"] for l in labels),
    }
    return labels, info


def labels_geojson(labels: list[dict]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [l["lon"], l["lat"]]},
             "properties": {"text": l["text"], "chainage_m": l["value"]}}
            for l in labels
        ],
    }
