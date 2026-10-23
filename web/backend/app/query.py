"""Spatial queries against the parcel tables (one per loaded state) and the village table (PostGIS).

The area of interest is matched against the GIST index; rows stream through a
server-side cursor straight into GeoJSON files, so memory use stays flat
however many parcels match.

For every matching row two areas are measured on the WGS84 ellipsoid
(geography, square metres): its full area, and the part of it that
falls inside the area of interest. The inside part is also written out as a
clipped geometry.
"""

import json
import time
from collections import defaultdict
from pathlib import Path

import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import config, typecast

# Property names added to every parcel; an original attribute with the same
# name is kept under an "attr_" prefix instead of being overwritten.
FIXED_KEYS = ("parcel_id", "village_id", "state", "source", "area_m2", "inside_m2", "inside_pct")

TOO_MANY = (
    "More than {limit:,} {noun} match. Use a smaller area, or restrict the "
    "states, or choose 'Fully inside' to reduce the result."
)


def connect() -> psycopg.Connection:
    """Read-only connection with a statement timeout: what searches use."""
    return psycopg.connect(**config.DB, options=config.DB_SESSION_OPTIONS, connect_timeout=10)


def extract_two_lines(aoi: dict) -> list[dict] | None:
    """If the uploaded area is made only of line work and holds exactly two
    lines (two LineStrings, or one MultiLineString of two parts), returns them;
    otherwise None."""
    lines: list[dict] = []
    for feature in aoi["features"]:
        geom = feature.get("geometry") or {}
        if geom.get("type") == "LineString":
            lines.append(geom)
        elif geom.get("type") == "MultiLineString":
            lines.extend({"type": "LineString", "coordinates": part} for part in geom["coordinates"])
        else:
            return None
    return lines if len(lines) == 2 else None


_STRIP_SQL = sql.SQL(
    """
WITH l AS (
  SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(a)s), 4326) AS a, ST_SetSRID(ST_GeomFromGeoJSON(%(b)s), 4326) AS b
), o AS (
  -- Line B is flipped when that pairs its ends with the nearer ends of line A,
  -- so the ring A -> B(reversed) -> back to A does not twist into a bow-tie.
  SELECT a, CASE WHEN ST_Distance(ST_StartPoint(a), ST_StartPoint(b)) + ST_Distance(ST_EndPoint(a), ST_EndPoint(b))
                    <= ST_Distance(ST_StartPoint(a), ST_EndPoint(b)) + ST_Distance(ST_EndPoint(a), ST_StartPoint(b))
                 THEN b ELSE ST_Reverse(b) END AS b
  FROM l
), s AS (
  SELECT ST_CollectionExtract(ST_MakeValid(ST_SetSRID(
           ST_MakePolygon(ST_MakeLine(ARRAY[a, ST_Reverse(b), ST_StartPoint(a)])), 4326)), 3) AS g
  FROM o
)
SELECT ST_AsGeoJSON(g, 9), ST_Area(g::geography), ST_IsEmpty(g) FROM s
"""
)

_UNION_SQL = sql.SQL(
    """
WITH u AS (
  SELECT ST_Union(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(x), 4326))) AS g
  FROM jsonb_array_elements_text(%(geoms)s::jsonb) AS t(x)
)
SELECT ST_AsGeoJSON(g, 9), CASE WHEN ST_Dimension(g) = 2 THEN ST_Area(g::geography) END FROM u
"""
)


def prepare_aoi(aoi: dict, lines: list[dict] | None) -> dict:
    """Reduces the upload to ONE geometry to match parcels against.

    With two lines this is the strip between them; otherwise the union of
    everything uploaded. Returns {"geometry": GeoJSON str, "area_m2": float|None,
    "strip": bool}."""
    try:
        with connect() as conn:
            if lines:
                row = conn.execute(_STRIP_SQL, {"a": json.dumps(lines[0]), "b": json.dumps(lines[1])}).fetchone()
                if row is None or row[0] is None or row[2] or not row[1]:
                    raise HTTPException(
                        400,
                        "The two lines do not enclose an area (they may cross each other or overlap). "
                        "Check that they run roughly side by side.",
                    )
                return {"geometry": row[0], "area_m2": row[1], "strip": True}
            geoms = [json.dumps(f["geometry"]) for f in aoi["features"] if f.get("geometry")]
            row = conn.execute(_UNION_SQL, {"geoms": json.dumps(geoms)}).fetchone()
            return {"geometry": row[0], "area_m2": row[1], "strip": False}
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error: {str(e).splitlines()[0]}")


def _where(mode: str, has_states: bool) -> sql.Composable:
    # The validity repair (v.g) is applied only to rows that already passed the
    # bbox/index filter: a few percent of source parcels have self-intersections,
    # which GEOS predicates other than Intersects can reject.
    parts = []
    if has_states:
        parts.append(sql.SQL("p.state = ANY(%(states)s)"))
    if mode == "within":
        parts.append(sql.SQL("p.geometry && a.g AND ST_Within(v.g, a.g)"))
    elif mode == "crossing":
        parts.append(sql.SQL("ST_Intersects(p.geometry, a.g) AND NOT ST_Within(v.g, a.g)"))
    else:
        parts.append(sql.SQL("ST_Intersects(p.geometry, a.g)"))
    return sql.SQL(" AND ").join(parts)


def _properties(row_id: int, id_key: str, state: str, source: str, attrs: dict, area: float, inside: float) -> dict:
    props = {
        id_key: row_id,
        "state": state,
        "source": source.split("/", 1)[-1],
        "area_m2": round(area, 2),
        "inside_m2": round(inside, 2),
        "inside_pct": round(100 * inside / area, 1) if area > 0 else 0.0,
    }
    # Type correction happens here, before anything is written: the numeric columns the
    # audit found (strings in the source files) go out as numbers.
    for key, value in (typecast.apply(source, attrs) or {}).items():
        props[f"attr_{key}" if key in FIXED_KEYS else key] = value
    return props


def table_exists(table: str) -> bool:
    try:
        with connect() as conn:
            return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{config.SCHEMA}.{table}",)).fetchone()[0]
    except psycopg.Error:
        return False


_VILLAGE_LOOKUP_SQL = sql.SQL(
    """
SELECT t.i, v.attrs->>'village', v.attrs->>'sub_district', v.attrs->>'district'
FROM unnest(%(xs)s::float8[], %(ys)s::float8[]) WITH ORDINALITY AS t(x, y, i)
LEFT JOIN LATERAL (
  SELECT vv.attrs FROM {}.{} vv
  WHERE ST_Intersects(vv.geometry, ST_SetSRID(ST_MakePoint(t.x, t.y), 4326))
  ORDER BY vv.id LIMIT 1
) v ON true
"""
)


def village_lookup(points: list[tuple[float, float] | None]) -> list[tuple[str | None, str | None, str | None]]:
    """For each point (lon, lat): (village name, taluka, district) of the village boundary that
    contains it; (None, None, None) where there is none or no point was given.

    The taluka is the village's sub-district. The points are the parcels' "points on surface"
    recorded at search time (always inside the parcel), so this needs only the permanent
    village table, not the parcel tables, which may be gone by then."""
    empty = (None, None, None)
    out = [empty] * len(points)
    usable = [(i, p) for i, p in enumerate(points) if p is not None]
    if not usable or not table_exists(config.VILLAGES_TABLE):
        return out
    statement = _VILLAGE_LOOKUP_SQL.format(sql.Identifier(config.SCHEMA), sql.Identifier(config.VILLAGES_TABLE))
    try:
        with connect() as conn:
            for start in range(0, len(usable), 10_000):
                chunk = usable[start : start + 10_000]
                params = {"xs": [p[0] for _, p in chunk], "ys": [p[1] for _, p in chunk]}
                for ordinal, village, taluka, district in conn.execute(statement, params):
                    out[chunk[ordinal - 1][0]] = (village, taluka, district)
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while looking up villages: {str(e).splitlines()[0]}")
    return out


_NO_PARCELS_SQL = sql.SQL(
    """
SELECT vv.id FROM {schema}.{villages} vv
WHERE vv.id = ANY(%(ids)s)
  AND NOT EXISTS (
    SELECT 1 FROM {schema}.{parcels} p
    WHERE p.geometry && vv.geometry AND ST_Intersects(ST_Centroid(p.geometry), vv.geometry)
  )
"""
)


def villages_without_parcels(parcel_table: str, village_ids: list[int]) -> list[int]:
    """Of these villages (ids in the village table), those that have no parcel in `parcel_table`.

    A parcel belongs to a village when its centroid lies inside the village boundary; a parcel that only
    touches the village's edge from the next village does not count. The bounding-box test lets the
    spatial index do most of the work, and the search for a village stops at its first parcel."""
    statement = _NO_PARCELS_SQL.format(
        schema=sql.Identifier(config.SCHEMA),
        villages=sql.Identifier(config.VILLAGES_TABLE),
        parcels=sql.Identifier(parcel_table),
    )
    found: list[int] = []
    try:
        with connect() as conn:
            for start in range(0, len(village_ids), 500):
                chunk = village_ids[start : start + 500]
                found.extend(r[0] for r in conn.execute(statement, {"ids": chunk}))
    except psycopg.errors.QueryCanceled:
        raise HTTPException(504, "Checking which villages have no parcels took too long.")
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while checking villages for parcels: {str(e).splitlines()[0]}")
    return found


def _check_not_too_many(
    conn: psycopg.Connection, aoi_geometry: str, states: list[str] | None, tables: list[str], limit: int, noun: str
) -> None:
    """Fails fast on a search that would match more than `limit` rows over all the tables.

    Counting the index hits is cheap; streaming and measuring a hundred thousand
    parcels only to then reject them took ~15 s. Exact for "Intersecting"; for the
    other modes the real result is a subset, so they are left to the streaming check."""
    total = 0
    for table in tables:
        count_sql = sql.SQL(
            "WITH a AS MATERIALIZED (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326) AS g) "
            "SELECT count(*) FROM (SELECT 1 FROM {}.{} p, a WHERE {} ST_Intersects(p.geometry, a.g) LIMIT %(limit)s) t"
        ).format(
            sql.Identifier(config.SCHEMA),
            sql.Identifier(table),
            sql.SQL("p.state = ANY(%(states)s) AND ") if states else sql.SQL(""),
        )
        params = {"aoi": aoi_geometry, "limit": limit + 1 - total}
        if states:
            params["states"] = states
        total += conn.execute(count_sql, params).fetchone()[0]
        if total > limit:
            raise HTTPException(413, TOO_MANY.format(limit=limit, noun=noun))


def _statement(table: str, mode: str, has_states: bool, decimals: int, with_point: bool) -> sql.Composable:
    # Each OFFSET 0 stops PostgreSQL from folding the helper subquery into its
    # callers, which would recompute ST_MakeValid / ST_Intersection once per
    # column that mentions them (it made this query 4-5x slower). Parcels that the
    # area fully covers skip the intersection altogether: inside = the whole parcel,
    # and no second copy of the geometry is fetched (it is reused as the clipped one).
    # with_point adds an interior point per row (kept for the village lookup of the CSV export).
    point_cols = "ST_X(pt.g), ST_Y(pt.g)" if with_point else "NULL::float8, NULL::float8"
    point_join = "CROSS JOIN LATERAL (SELECT ST_PointOnSurface(v.g) AS g OFFSET 0) pt" if with_point else ""
    return (
        sql.SQL(
            """
WITH a AS MATERIALIZED (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326) AS g)
SELECT p.id, p.state, p.source, p.attrs, ST_AsGeoJSON(p.geometry, {decimals}),
       v.area, x.inside, x.gj, v.covered,
       ST_XMin(p.geometry), ST_YMin(p.geometry), ST_XMax(p.geometry), ST_YMax(p.geometry),
       {point_cols}
FROM {schema}.{table} p
CROSS JOIN a
CROSS JOIN LATERAL (
  -- Polygon parts only, and only when the repair produced a mixed collection:
  -- extracting from an ordinary repaired polygon changed 36 of 12,288 test parcels
  -- enough to flip them from "fully inside" to "crossing".
  SELECT CASE WHEN GeometryType(m) = 'GEOMETRYCOLLECTION' THEN ST_CollectionExtract(m, 3) ELSE m END AS g
  FROM (SELECT ST_MakeValid(p.geometry) AS m OFFSET 0) q0
  OFFSET 0
) v0
CROSS JOIN LATERAL (SELECT v0.g AS g, ST_Area(v0.g::geography) AS area, ST_Covers(a.g, v0.g) AS covered OFFSET 0) v
CROSS JOIN LATERAL (
  SELECT CASE WHEN v.covered THEN v.area ELSE ST_Area(i::geography) END AS inside,
         CASE WHEN v.covered OR ST_IsEmpty(i) THEN NULL ELSE ST_AsGeoJSON(i, {decimals}) END AS gj
  FROM (SELECT CASE WHEN v.covered THEN NULL ELSE ST_CollectionExtract(ST_Intersection(v.g, a.g), 3) END AS i OFFSET 0) q
  OFFSET 0
) x
{point_join}
WHERE """
        ).format(
            schema=sql.Identifier(config.SCHEMA),
            table=sql.Identifier(table),
            decimals=sql.SQL(str(int(decimals))),
            point_cols=sql.SQL(point_cols),
            point_join=sql.SQL(point_join),
        )
        + _where(mode, has_states)
    )


def run_query(
    aoi_geometry: str,
    mode: str,
    out_path: Path,
    clipped_path: Path,
    tables: list[str],
    states: list[str] | None = None,
    limit: int = config.MAX_FEATURES,
    decimals: int = 7,
    noun: str = "parcels",
    id_key: str = "parcel_id",
    with_point: bool = False,
) -> dict:
    """Matches the given tables (the loaded state tables, or the village table) against the area
    and writes the matches, and their inside-the-area parts, to two GeoJSON files.

    `limit` caps the total number of rows; `decimals` is the coordinate precision written
    (villages are heavy, so fewer); `states` optionally restricts the rows by their state column."""
    params = {"aoi": aoi_geometry}
    if states:
        params["states"] = states

    t0 = time.time()
    # [count, area m2, area inside m2]
    per_state: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    per_source: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    total = [0, 0.0, 0.0]
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    count = clipped_count = 0

    try:
        with connect() as conn, open(out_path, "w", encoding="utf-8") as out, open(clipped_path, "w", encoding="utf-8") as clip_out:
            if mode == "intersects" and tables:
                _check_not_too_many(conn, aoi_geometry, states, tables, limit, noun)
            head = '{"type":"FeatureCollection","features":['
            out.write(head)
            clip_out.write(head)
            for number, table in enumerate(tables):
                with conn.cursor(name=f"stream_{number}") as cur:
                    cur.itersize = 2000
                    cur.execute(_statement(table, mode, bool(states), decimals, with_point), params)
                    for row in cur:
                        if count >= limit:
                            raise HTTPException(413, TOO_MANY.format(limit=limit, noun=noun))
                        pid, state, source, attrs, geom_json, area, inside, clip_json, covered, x0, y0, x1, y1, px, py = row
                        area = area or 0.0
                        inside = inside or 0.0
                        props_json = json.dumps(_properties(pid, id_key, state, source, attrs, area, inside), ensure_ascii=False)
                        # "pt" is a foreign member (ignored by GIS readers): the interior point used for the village lookup.
                        point = ',"pt":[%.7f,%.7f]' % (px, py) if px is not None and py is not None else ""
                        out.write(("," if count else "") + '{"type":"Feature","id":%d,"geometry":%s,"properties":%s%s}' % (pid, geom_json, props_json, point))
                        # A parcel the area fully covers is its own clipped part.
                        clip_geom = geom_json if covered else clip_json
                        if clip_geom is not None and inside > 0:
                            clip_out.write(("," if clipped_count else "") + '{"type":"Feature","id":%d,"geometry":%s,"properties":%s}' % (pid, clip_geom, props_json))
                            clipped_count += 1
                        count += 1
                        short = source.split("/", 1)[-1]
                        for bucket in (per_state[state], per_source[short], total):
                            bucket[0] += 1
                            bucket[1] += area
                            bucket[2] += inside
                        minx, miny, maxx, maxy = min(minx, x0), min(miny, y0), max(maxx, x1), max(maxy, y1)
            out.write("]}")
            clip_out.write("]}")
    except psycopg.errors.QueryCanceled:
        out_path.unlink(missing_ok=True)
        clipped_path.unlink(missing_ok=True)
        raise HTTPException(504, "The query took too long. Use a smaller area or restrict the states.")
    except psycopg.Error as e:
        out_path.unlink(missing_ok=True)
        clipped_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Database error: {str(e).splitlines()[0]}")
    except HTTPException:
        # Too many rows: drop the partial files.
        out_path.unlink(missing_ok=True)
        clipped_path.unlink(missing_ok=True)
        raise

    def pack(b: list[float]) -> dict:
        return {"count": int(b[0]), "area_m2": round(b[1], 2), "inside_m2": round(b[2], 2)}

    return {
        "count": count,
        "per_state": {k: int(v[0]) for k, v in per_state.items()},
        "per_source": {k: int(v[0]) for k, v in per_source.items()},
        "stats": {
            "total": pack(total),
            "per_state": {k: pack(v) for k, v in per_state.items()},
            "per_source": {k: pack(v) for k, v in per_source.items()},
        },
        "clipped_count": clipped_count,
        "bbox": [minx, miny, maxx, maxy] if count else None,
        "seconds": round(time.time() - t0, 2),
    }
