"""Layer priority.

A state's parcels come from one or more source files (layers) that can map the same land. The user ranks the layers:
every parcel of the top layer is kept, and from each next layer only the parcels that the layers above do not already
cover are added. A parcel counts as already covered when more than DROP_FRACTION of its area lies inside the parcels of
the higher layers that were kept.

Everything is computed in PostGIS against the state's parcel table (spatial index), so it works for any number of parcels.
"""

from collections import defaultdict

import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import config, query, statedata

DROP_FRACTION = 0.5  # a lower-priority parcel with more than this share of its area under higher-priority parcels is left out
BATCH = 1000

_SHORT = "regexp_replace(p.source, '^[^/]*/', '')"

_CANDIDATES = sql.SQL(
    "SELECT p.id, " + _SHORT + " FROM {t} p WHERE ST_Intersects(p.geometry, ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326))"
)

_COVERED = sql.SQL(
    """
SELECT lo.id,
       COALESCE(ST_Area(ST_CollectionExtract(ST_Intersection(lo.v, u.g), 3)::geography), 0)
         / NULLIF(ST_Area(lo.v::geography), 0)
FROM (SELECT p.id, p.geometry AS raw, ST_MakeValid(p.geometry) AS v FROM {t} p WHERE p.id = ANY(%(ids)s)) lo
CROSS JOIN LATERAL (
  SELECT ST_Union(ST_MakeValid(h.geometry)) AS g
  FROM {t} h
  WHERE h.geometry && lo.raw
    AND ST_Intersects(h.geometry, lo.raw)
    AND regexp_replace(h.source, '^[^/]*/', '') = ANY(%(higher)s)
    AND NOT (h.id = ANY(%(dropped)s))
) u
WHERE u.g IS NOT NULL
"""
)


def _ranks(order: list[str], found: set[str]) -> dict[str, int]:
    """Rank of every source (0 = highest). Sources the order does not mention go after it, by name."""
    ranks = {s: i for i, s in enumerate(order)}
    for s in sorted(found - set(ranks)):
        ranks[s] = len(ranks)
    return ranks


def excluded_ids(table: str, aoi_geometry: str, order: list[str]) -> tuple[set[int], dict[str, int]]:
    """The parcels of one state table (inside or touching the area) to leave out, and how many that is per source."""
    if len(order) < 2:
        return set(), {}
    t = sql.Identifier(config.SCHEMA, table)
    try:
        with query.connect() as conn:
            by_source: dict[str, list[int]] = defaultdict(list)
            for pid, src in conn.execute(_CANDIDATES.format(t=t), {"aoi": aoi_geometry}):
                by_source[src].append(pid)
            ranks = _ranks(order, set(by_source))
            by_rank = sorted(by_source.items(), key=lambda kv: ranks[kv[0]])
            excluded: set[int] = set()
            dropped: dict[str, int] = {}
            for src, ids in by_rank:
                higher = [s for s, r in ranks.items() if r < ranks[src]]
                if not higher:
                    continue  # the top layer keeps everything
                gone = 0
                for start in range(0, len(ids), BATCH):
                    chunk = ids[start : start + BATCH]
                    for pid, fraction in conn.execute(
                        _COVERED.format(t=t), {"ids": chunk, "higher": higher, "dropped": list(excluded)}
                    ):
                        if fraction is not None and fraction > DROP_FRACTION:
                            excluded.add(pid)
                            gone += 1
                if gone:
                    dropped[src] = gone
            return excluded, dropped
    except psycopg.errors.QueryCanceled:
        raise HTTPException(504, "Applying the layer priority took too long. Use a smaller area.")
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while applying the layer priority: {str(e).splitlines()[0]}")


def available_sources(aoi_geometry: str) -> dict:
    """The layers (source files) of the states the area falls in. For a state whose parcels are already in the database,
    only the layers that have parcels in the area, with their counts; for one that is not loaded yet, all its files."""
    detected = statedata.detect_states(aoi_geometry)
    folders = list(dict.fromkeys(d["folder"] for d in detected if d["folder"]))
    ready = statedata.ready_tables(folders)
    sources: dict[str, dict] = {}
    states = []
    for d in detected:
        folder = d["folder"]
        if not folder:
            continue
        states.append({"name": d["name"], "folder": folder, "loaded": folder in ready})
        if folder in ready:
            try:
                with query.connect() as conn:
                    rows = conn.execute(
                        sql.SQL(
                            "SELECT " + _SHORT + ", count(*) FROM {t} p WHERE ST_Intersects(p.geometry, ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326)) GROUP BY 1"
                        ).format(t=sql.Identifier(config.SCHEMA, ready[folder])),
                        {"aoi": aoi_geometry},
                    ).fetchall()
            except psycopg.Error as e:
                raise HTTPException(500, f"Database error while listing the layers: {str(e).splitlines()[0]}")
            found = {s: int(n) for s, n in rows}
        else:
            found = {s: None for s in statedata.source_files(folder)}
        for s, n in found.items():
            entry = sources.setdefault(s, {"source": s, "states": [], "parcels": None})
            entry["states"].append(d["name"])
            if n is not None:
                entry["parcels"] = (entry["parcels"] or 0) + n
    return {"sources": sorted(sources.values(), key=lambda e: e["source"].lower()), "states": states}
