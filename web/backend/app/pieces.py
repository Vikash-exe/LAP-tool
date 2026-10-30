"""Separate pieces of a parcel inside the searched area.

A parcel that the area cuts into several separate pieces is one MultiPolygon in clipped.geojson. Each piece becomes its own
feature there, with its own affected area, so the map, the parcel table and the downloads list every piece on its own row.
Pieces of one parcel share its id and its properties; `part` ("1 of 2") and `part_count` tell them apart. A parcel in one
piece is left as it is.
"""

import json
from pathlib import Path

import psycopg
from fastapi import HTTPException

from . import query

_AREAS_SQL = """
SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(t.g), 4326)::geography)
FROM unnest(%(geoms)s::text[]) WITH ORDINALITY AS t(g, n)
ORDER BY t.n
"""


def split(result_dir: Path) -> int | None:
    """Splits the multi-part features of clipped.geojson in place. Returns the number of features it now holds
    (None when there is no clipped file)."""
    path = result_dir / "clipped.geojson"
    if not path.exists():
        return None
    fc = json.loads(path.read_text(encoding="utf-8"))
    features = fc["features"]
    if not any(f["geometry"]["type"] == "MultiPolygon" and len(f["geometry"]["coordinates"]) > 1 for f in features):
        return len(features)

    # (feature index, polygon coordinates) of every piece of a split parcel, in order
    todo: list[tuple[int, list]] = [
        (i, poly)
        for i, f in enumerate(features)
        if f["geometry"]["type"] == "MultiPolygon" and len(f["geometry"]["coordinates"]) > 1
        for poly in f["geometry"]["coordinates"]
    ]
    try:
        with query.connect() as conn:
            areas = [r[0] for r in conn.execute(_AREAS_SQL, {"geoms": [json.dumps({"type": "Polygon", "coordinates": p}) for _, p in todo]})]
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while splitting the parcels into pieces: {str(e).splitlines()[0]}")

    by_feature: dict[int, list[tuple[list, float]]] = {}
    for (i, poly), area in zip(todo, areas):
        by_feature.setdefault(i, []).append((poly, area or 0.0))

    out: list[dict] = []
    for i, f in enumerate(features):
        pieces = by_feature.get(i)
        if not pieces:
            out.append(f)
            continue
        # the larger piece first
        pieces.sort(key=lambda t: -t[1])
        for n, (poly, area) in enumerate(pieces, start=1):
            props = dict(f["properties"])
            props["inside_m2"] = round(area, 2)
            total = props.get("area_m2")
            if total:
                props["inside_pct"] = round(100.0 * area / total, 2)
            props["part"] = f"{n} of {len(pieces)}"
            props["part_count"] = len(pieces)
            out.append({"type": "Feature", "id": f.get("id"), "geometry": {"type": "Polygon", "coordinates": poly}, "properties": props})
    fc["features"] = out
    path.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(out)
