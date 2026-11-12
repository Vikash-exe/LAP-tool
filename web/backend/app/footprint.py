"""Where each layer (parquet file) has parcels: a coarse footprint, used to show the layers on a map and to find the ones
that cover an area.

For every file the first vertex of every parcel is counted in a grid of CELL degrees (about 2 km). The sync script
(src/sync_parcels.py) computes it when it loads a file and stores it in the catalog (cadastral.parcel_files.footprint);
the web app only reads it from there.
"""
from __future__ import annotations

import json
import struct
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql

from . import config, statedata

CELL = 0.02  # degrees (about 2 km)
COARSE = 5  # the map preview merges 5 x 5 cells (0.1 degree)


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


def compute(path: Path) -> dict:
    """The footprint of one parquet file: parcels per grid cell, the bounding box and the row count."""
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
    return {
        "cell": CELL,
        "rows": rows,
        "bbox": [minx, miny, maxx, maxy] if rows else None,
        "cells": [[ix, iy, n] for (ix, iy), n in cells.items()],
    }


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
    """The footprint of every layer of a state (from the catalog), and how many parcels of each lie near the searched area."""
    area, bbox = area_cells(geometry_json)
    rows = statedata._rows(
        sql.SQL("SELECT name, footprint FROM {} WHERE state = %s ORDER BY name").format(sql.Identifier(config.SCHEMA, config.CATALOG)),
        (folder,),
    )
    out = []
    for name, data in rows:
        if not data:
            out.append({"name": name, "state": "error", "error": "extent not read yet (run the sync script again)"})
            continue
        near = sum(n for ix, iy, n in data["cells"] if (ix, iy) in area)
        coarse: dict[tuple[int, int], int] = defaultdict(int)
        for ix, iy, n in data["cells"]:
            coarse[(ix // COARSE, iy // COARSE)] += n
        out.append(
            {
                "name": name,
                "state": "ready",
                "rows": data["rows"],
                "bbox": data["bbox"],
                "near": near,
                "cells": [[ix, iy, n] for (ix, iy), n in coarse.items()],
            }
        )
    return {"folder": folder, "cell": CELL * COARSE, "files": out, "area_bbox": bbox, "computing": False}
