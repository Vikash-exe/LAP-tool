"""Where each parquet file (layer) has parcels: a coarse footprint, used to show the layers on a map and to find the ones that
cover an area, so that only those need to be loaded.

For every file the first vertex of every parcel is counted in a grid of CELL degrees (about 2 km). Reading takes a few
seconds per million parcels; the result is cached in data/footprints/<state>/<file>.json (keyed by file size and time),
so it is done once per file, in a background thread.
"""
from __future__ import annotations

import json
import struct
import threading
import time
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import config, statedata

CELL = 0.02  # degrees (about 2 km)
COARSE = 5  # the map preview merges 5 x 5 cells (0.1 degree)
CACHE_DIR = config.WEB_ROOT / "data" / "footprints"

_lock = threading.Lock()
_progress: dict[str, dict] = {}  # "<folder>/<file>" -> {"state": queued|computing|error, "done": rows, "total": rows, "error": str}
_worker_running = False


def _cache_path(folder: str, name: str) -> Path:
    return CACHE_DIR / folder / (name + ".json")


def _stamp(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_size, int(st.st_mtime)]


def cached(folder: str, path: Path) -> dict | None:
    f = _cache_path(folder, path.name)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if data.get("stamp") == _stamp(path) and data.get("cell") == CELL else None
    except (OSError, ValueError):
        return None


def _first_vertices(arr: pa.Array):
    """(x, y) of the first vertex of every WKB polygon / multipolygon in a binary array (nulls skipped)."""
    if pa.types.is_large_binary(arr.type):
        fmt_off, size = "q", 8
    else:
        fmt_off, size = "i", 4
    bufs = arr.buffers()
    offsets, data = memoryview(bufs[1]), memoryview(bufs[2])
    n = len(arr)
    base = arr.offset
    valid = arr.is_valid().to_pylist() if arr.null_count else None
    for i in range(n):
        if valid is not None and not valid[i]:
            continue
        o = struct.unpack_from(fmt_off, offsets, (base + i) * size)[0]
        endian = "<" if data[o] == 1 else ">"
        wkb_type = struct.unpack_from(endian + "I", data, o + 1)[0] & 0xFFFF
        if wkb_type == 6:  # multipolygon: count, then the first polygon's own header
            p = o + 9
            endian = "<" if data[p] == 1 else ">"
            p += 5
        elif wkb_type == 3:
            p = o + 5
        else:
            continue
        try:
            yield struct.unpack_from(endian + "dd", data, p + 8)  # skip ring count and point count
        except struct.error:
            continue


def compute(folder: str, path: Path, on_rows=None) -> dict:
    pf = pq.ParquetFile(path)
    cells: dict[tuple[int, int], int] = defaultdict(int)
    rows = 0
    minx = miny = 1e9
    maxx = maxy = -1e9
    for rg in range(pf.metadata.num_row_groups):
        col = pf.read_row_group(rg, columns=["geometry"]).column(0).combine_chunks()
        for x, y in _first_vertices(col):
            if not (-180 <= x <= 180 and -90 <= y <= 90):
                continue
            cells[(int(x // CELL), int(y // CELL))] += 1
            rows += 1
            minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
        cancel_rows = pf.metadata.row_group(rg).num_rows
        if on_rows:
            on_rows(cancel_rows)
    data = {
        "stamp": _stamp(path),
        "cell": CELL,
        "rows": rows,
        "bbox": [minx, miny, maxx, maxy] if rows else None,
        "cells": [[ix, iy, n] for (ix, iy), n in cells.items()],
    }
    f = _cache_path(folder, path.name)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(f)
    return data


def _worker(todo: list[tuple[str, Path]]) -> None:
    global _worker_running
    try:
        for folder, path in todo:
            key = f"{folder}/{path.name}"
            total = pq.ParquetFile(path).metadata.num_rows
            _progress[key] = {"state": "computing", "done": 0, "total": total}

            def bump(n: int, key=key) -> None:
                _progress[key]["done"] += n

            try:
                compute(folder, path, bump)
                _progress.pop(key, None)
            except Exception as e:  # one unreadable file must not stop the others
                _progress[key] = {"state": "error", "error": str(e).splitlines()[0] if str(e) else type(e).__name__}
    finally:
        with _lock:
            _worker_running = False


def ensure_computing(folder: str) -> None:
    """Start (once) the background computation of the footprints this state's files still lack."""
    global _worker_running
    files = statedata.state_folders().get(folder, {}).get("files", [])
    todo = [(folder, p) for p in files if cached(folder, p) is None and _progress.get(f"{folder}/{p.name}", {}).get("state") != "error"]
    if not todo:
        return
    with _lock:
        if _worker_running:
            for fo, p in todo:
                _progress.setdefault(f"{fo}/{p.name}", {"state": "queued"})
            return
        _worker_running = True
        for fo, p in todo:
            _progress[f"{fo}/{p.name}"] = {"state": "queued"}
    threading.Thread(target=_worker, args=(todo,), name="footprints", daemon=True).start()


# ---- the searched area ---------------------------------------------------------------


def _coords(geom: dict):
    c = geom.get("coordinates")
    if geom["type"] == "GeometryCollection":
        for g in geom["geometries"]:
            yield from _coords(g)
        return

    def walk(v):
        if v and isinstance(v[0], (int, float)):
            yield v
        else:
            for x in v:
                yield from walk(x)

    yield from walk(c)


def area_cells(geometry_json: str) -> tuple[set[tuple[int, int]], list[float]]:
    """The cells the searched area touches (a line: along it, plus one cell around; a polygon: its whole bounding box),
    and its bounding box."""
    geom = json.loads(geometry_json)
    pts = [(p[0], p[1]) for p in _coords(geom)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    bbox = [min(xs), min(ys), max(xs), max(ys)]
    has_polygon = "Polygon" in json.dumps({"t": _types(geom)})
    cells: set[tuple[int, int]] = set()
    if has_polygon:
        for ix in range(int(bbox[0] // CELL), int(bbox[2] // CELL) + 1):
            for iy in range(int(bbox[1] // CELL), int(bbox[3] // CELL) + 1):
                cells.add((ix, iy))
        return cells, bbox
    # a line: walk along it in steps of half a cell, so no cell is skipped between distant vertices
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] or pts):
        steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) / (CELL / 2)))
        for k in range(steps + 1):
            x, y = x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps
            cx, cy = int(x // CELL), int(y // CELL)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cells.add((cx + dx, cy + dy))
    if len(pts) == 1:
        cx, cy = int(pts[0][0] // CELL), int(pts[0][1] // CELL)
        cells.update((cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    return cells, bbox


def _types(geom: dict) -> list[str]:
    if geom["type"] == "GeometryCollection":
        return [t for g in geom["geometries"] for t in _types(g)]
    return [geom["type"]]


def describe(folder: str, geometry_json: str) -> dict:
    """The footprint of every file of a state, and how many parcels of each lie near the searched area."""
    ensure_computing(folder)
    area, bbox = area_cells(geometry_json)
    out = []
    for path in statedata.state_folders().get(folder, {}).get("files", []):
        data = cached(folder, path)
        key = f"{folder}/{path.name}"
        if data is None:
            prog = _progress.get(key) or {"state": "queued"}
            out.append({"name": path.name, "state": prog["state"], "done": prog.get("done"), "total": prog.get("total"), "error": prog.get("error")})
            continue
        near = sum(n for ix, iy, n in data["cells"] if (ix, iy) in area)
        coarse: dict[tuple[int, int], int] = defaultdict(int)
        for ix, iy, n in data["cells"]:
            coarse[(ix // COARSE, iy // COARSE)] += n
        out.append(
            {
                "name": path.name,
                "state": "ready",
                "rows": data["rows"],
                "bbox": data["bbox"],
                "near": near,
                "cells": [[ix, iy, n] for (ix, iy), n in coarse.items()],
            }
        )
    return {"folder": folder, "cell": CELL * COARSE, "files": out, "area_bbox": bbox, "computing": any(f["state"] != "ready" and f["state"] != "error" for f in out)}
