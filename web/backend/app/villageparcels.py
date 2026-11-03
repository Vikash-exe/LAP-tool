"""Every parcel of the villages a search found ("whole village parcels").

The search itself returns the parcels that meet the area. This gives, on request, all the parcels that lie in the
villages the area passes through, so the whole village can be taken for a plan. A parcel belongs to a village when its
centroid lies inside the village boundary (the same rule as the villages-without-parcels check). The layer priority of
the search is applied, so there is one parcel per place.

The result is a small result folder of its own, result_dir/village_parcels/, with the same files the exports read
(result.geojson, meta.json), so the normal export code makes the shapefile, KML, CSV and so on from it. It is built the
first time it is asked for and reused after.
"""

import json
import shutil
from collections import defaultdict
from pathlib import Path

import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import config, enrich, loader, priority, query, statedata

SUBDIR = "village_parcels"

_IDS_SQL = sql.SQL(
    """
SELECT DISTINCT ON (p.id) p.id, regexp_replace(p.source, '^[^/]*/', '')
FROM {schema}.{parcels} p
JOIN {schema}.{villages} vv
  ON vv.id = ANY(%(villages)s) AND p.geometry && vv.geometry AND ST_Intersects(ST_Centroid(p.geometry), vv.geometry)
ORDER BY p.id
"""
)

# a = the searched area (NULL for a line or point search), v = the repaired parcel; inside = the part of it in the area.
_ROWS_SQL = sql.SQL(
    """
WITH a AS MATERIALIZED (
  SELECT ST_SetSRID(ST_Union(ST_GeomFromGeoJSON(g)), 4326) AS g FROM unnest(%(aoi)s::text[]) g
)
SELECT p.id, p.state, p.source, p.attrs, ST_AsGeoJSON(p.geometry, 7),
       v.area, x.inside, ST_X(pt.g), ST_Y(pt.g)
FROM {schema}.{parcels} p
CROSS JOIN a
CROSS JOIN LATERAL (
  SELECT CASE WHEN GeometryType(m) = 'GEOMETRYCOLLECTION' THEN ST_CollectionExtract(m, 3) ELSE m END AS g
  FROM (SELECT ST_MakeValid(p.geometry) AS m OFFSET 0) q0 OFFSET 0
) v0
CROSS JOIN LATERAL (SELECT v0.g AS g, ST_Area(v0.g::geography) AS area OFFSET 0) v
CROSS JOIN LATERAL (
  SELECT CASE WHEN a.g IS NOT NULL AND p.geometry && a.g AND ST_Intersects(v.g, a.g)
              THEN COALESCE(ST_Area(ST_CollectionExtract(ST_Intersection(v.g, a.g), 3)::geography), 0)
              ELSE 0 END AS inside
  OFFSET 0
) x
CROSS JOIN LATERAL (SELECT ST_PointOnSurface(v.g) AS g OFFSET 0) pt
WHERE p.id = ANY(%(ids)s)
ORDER BY p.id
"""
)


def _aoi_geometries(result_dir: Path) -> list[str]:
    """The searched area's polygons as GeoJSON text (lines and points have no area, so they are left out)."""
    path = result_dir / "aoi.geojson"
    if not path.exists():
        return []
    fc = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for f in fc.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") in ("Polygon", "MultiPolygon"):
            out.append(json.dumps(g))
    return out


def build(result_dir: Path, meta: dict, order: list[str] | None = None) -> Path:
    """Makes (or reuses) result_dir/village_parcels/ and returns it."""
    sub = result_dir / SUBDIR
    if (sub / "result.geojson").exists() and (sub / "meta.json").exists():
        return sub
    vfile = result_dir / "villages.geojson"
    if not (meta.get("villages") or {}).get("count") or not vfile.exists():
        raise HTTPException(404, "This search found no villages.")
    order = order or ((meta.get("priority") or {}).get("order") or [])

    features = json.loads(vfile.read_text(encoding="utf-8"))["features"]
    ids_by_folder: dict[str, list[int]] = defaultdict(list)
    for f in features:
        ids_by_folder[f["properties"]["state"]].append(f["properties"]["village_id"])
    aoi = _aoi_geometries(result_dir)

    tmp = result_dir / (SUBDIR + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    folders = list(ids_by_folder)
    count = 0
    per_source: dict[str, int] = defaultdict(int)
    per_state: dict[str, int] = defaultdict(int)
    dropped: dict[str, int] = defaultdict(int)
    missing: list[str] = []
    try:
        with statedata.using(folders):
            with open(tmp / "result.geojson", "w", encoding="utf-8") as out, query.connect() as conn:
                out.write('{"type":"FeatureCollection","features":[')
                for folder, village_ids in ids_by_folder.items():
                    if not statedata.source_files(folder):
                        missing.append(folder)
                        continue
                    table = loader.ensure_loaded(folder)  # reloads the state if its table was dropped meanwhile
                    schema = sql.Identifier(config.SCHEMA)
                    by_source: dict[str, list[int]] = defaultdict(list)
                    for pid, src in conn.execute(
                        _IDS_SQL.format(schema=schema, parcels=sql.Identifier(table), villages=sql.Identifier(config.VILLAGES_TABLE)),
                        {"villages": village_ids},
                    ):
                        by_source[src].append(pid)
                    skip: set[int] = set()
                    if len(order) >= 2:
                        skip, gone = priority.excluded_among(table, by_source, order)
                        for s, n in gone.items():
                            dropped[s] += n
                    keep = sorted(pid for ids in by_source.values() for pid in ids if pid not in skip)
                    if count + len(keep) > config.MAX_FEATURES:
                        raise HTTPException(413, query.TOO_MANY.format(limit=config.MAX_FEATURES, noun="parcels in these villages"))
                    ranks = {s: i + 1 for i, s in enumerate(order)} if len(order) >= 2 else {}
                    statement = _ROWS_SQL.format(schema=schema, parcels=sql.Identifier(table))
                    for start in range(0, len(keep), 2000):
                        for pid, state, source, attrs, geom_json, area, inside, px, py in conn.execute(
                            statement, {"aoi": aoi, "ids": keep[start : start + 2000]}
                        ):
                            area = area or 0.0
                            inside = min(inside or 0.0, area)
                            props = query._properties(pid, "parcel_id", state, source, attrs, area, inside, area > 0 and inside >= area * 0.9999, ranks.get(source.split("/", 1)[-1]))
                            point = ',"pt":[%.7f,%.7f]' % (px, py) if px is not None and py is not None else ""
                            out.write(("," if count else "") + '{"type":"Feature","id":%d,"geometry":%s,"properties":%s%s}' % (pid, geom_json, json.dumps(props, ensure_ascii=False), point))
                            count += 1
                            per_source[source.split("/", 1)[-1]] += 1
                            per_state[state] += 1
                statedata.touch(folders)
                out.write("]}")
        enrich.enrich_parcels(tmp)
        (tmp / "meta.json").write_text(
            json.dumps({"count": count, "per_source": dict(per_source), "per_state": dict(per_state), "dropped": dict(dropped), "missing_states": missing}),
            encoding="utf-8",
        )
        if count == 0:
            raise HTTPException(404, "No parcel lies in these villages" + (f" (no parcel data for: {', '.join(missing)})." if missing else "."))
        shutil.rmtree(sub, ignore_errors=True)
        tmp.rename(sub)
        return sub
    except psycopg.errors.QueryCanceled:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(504, "Collecting the parcels of the villages took too long. Use fewer villages.")
    except psycopg.Error as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"Database error while collecting the village parcels: {str(e).splitlines()[0]}")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
