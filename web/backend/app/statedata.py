"""Which parcel data is in the database.

All parcel data lives permanently in PostgreSQL: one table per state, cadastral.parcels_<state>, filled by
src/sync_parcels.py. The catalog table cadastral.parcel_files has one row per source file (layer) that was loaded:
its state, table, size, rows and its footprint (where it has parcels). The web app only reads; it never loads or drops
anything. This module finds the states an area falls in and answers "what is in the database" from the catalog.
"""

import re
import unicodedata

import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import cancel, config

# Boundary-file name (normalised) -> parcel folder name (normalised), where the two
# spell the same state differently.
_ALIASES = {
    "odisha": "odissa",
    "puducherry": "pondicherry",
    "lakshadweep": "lakshadeep",
    "chhattisgarh": "chhattishgarh",
    "jammuandkashmir": "jandk",  # the folder is "J&K"
}

# One row per source file loaded into a state table. `source` is the path under the data folder ("Goa/file.parquet"),
# `name` the file name the dashboard shows as the layer.
CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS {catalog} (
    state       text   NOT NULL,
    source      text   NOT NULL,
    name        text   NOT NULL,
    table_name  text   NOT NULL,
    size_bytes  bigint NOT NULL,
    mtime       bigint NOT NULL,
    rows        bigint NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    footprint   jsonb,
    PRIMARY KEY (state, source)
)
"""


def connect_rw() -> psycopg.Connection:
    """A connection that may create tables (the search connection is read-only)."""
    return cancel.track(psycopg.connect(**config.DB, connect_timeout=10))


def ensure_catalog(conn: psycopg.Connection) -> None:
    """Create the schema and the catalog table in a new database (does nothing, and needs no privileges, once they exist)."""
    if conn.execute("SELECT to_regclass(%s)", (f"{config.SCHEMA}.{config.CATALOG}",)).fetchone()[0]:
        return
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(config.SCHEMA)))
    conn.execute(sql.SQL(CATALOG_DDL).format(catalog=sql.Identifier(config.SCHEMA, config.CATALOG)))
    conn.commit()


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def table_name(folder: str) -> str:
    """cadastral.<this> holds the parcels of the state folder."""
    return "parcels_" + (re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", folder).encode("ascii", "ignore").decode().lower()).strip("_") or "state")


# ---- what the catalog says is in the database ----------------------------------------


def _rows(query: sql.Composable, params=()) -> list[tuple]:
    try:
        with connect_rw() as conn:
            ensure_catalog(conn)
            return conn.execute(query, params).fetchall()
    except psycopg.Error as e:
        raise HTTPException(500, f"Could not read the parcel catalog: {str(e).splitlines()[0]}")


def catalog() -> list[dict]:
    """Every loaded source file (layer), by state and name."""
    rows = _rows(
        sql.SQL("SELECT state, source, name, table_name, size_bytes, rows, loaded_at FROM {} ORDER BY state, name").format(
            sql.Identifier(config.SCHEMA, config.CATALOG)
        )
    )
    return [
        {"state": s, "source": src, "name": n, "table_name": t, "size_bytes": int(b), "rows": int(r), "loaded_at": at.isoformat()}
        for s, src, n, t, b, r, at in rows
    ]


def states_in_database() -> list[dict]:
    """[{state, table_name, files, rows}] for every state that has parcel data."""
    rows = _rows(
        sql.SQL("SELECT state, min(table_name), count(*), sum(rows) FROM {} GROUP BY state ORDER BY state").format(
            sql.Identifier(config.SCHEMA, config.CATALOG)
        )
    )
    return [{"state": s, "table_name": t, "files": int(n), "rows": int(r or 0)} for s, t, n, r in rows]


def ready_tables(folders: list[str]) -> dict[str, str]:
    """{state: table name} for those of the given states that have parcel data."""
    if not folders:
        return {}
    return {e["state"]: e["table_name"] for e in states_in_database() if e["state"] in folders}


def source_files(folder: str) -> list[str]:
    """The layers (source file names) of a state that are in the database."""
    rows = _rows(sql.SQL("SELECT name FROM {} WHERE state = %s ORDER BY name").format(sql.Identifier(config.SCHEMA, config.CATALOG)), (folder,))
    return [r[0] for r in rows]


def has_data() -> bool:
    """True when the database holds parcel data at all (the sync script has been run)."""
    return bool(states_in_database())


def folder_for_boundary(name: str) -> str | None:
    """The parcel state matching a state name from the boundary file, or None when that state has no parcel data."""
    key = _norm(name)
    key = _ALIASES.get(key, key)
    for e in states_in_database():
        if _norm(e["state"]) == key:
            return e["state"]
    return None


# ---- which states an area touches ----------------------------------------------------


def detect_states(aoi_geometry: str) -> list[dict]:
    """The states whose boundary the area intersects or lies within BOUNDARY_TOLERANCE_M of.

    Returns [{"name": boundary name, "folder": parcel state or None, "inside": bool}]."""
    query = sql.SQL(
        """
-- Planar and on a simplified copy of the area (about 55 m tolerance): the spheroid distance to a state
-- outline of over a million points took minutes for an area with a few thousand vertices. The margin is
-- BOUNDARY_TOLERANCE_M turned into degrees (about 111 km per degree), which is all a border check needs.
WITH a AS (SELECT ST_SimplifyPreserveTopology(ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326), 0.0005) AS g)
SELECT s.name, ST_Intersects(s.geometry, a.g)
FROM {}.{} s, a
WHERE s.geometry && ST_Expand(a.g, %(deg)s * 2)
  AND ST_DWithin(s.geometry, a.g, %(deg)s)
ORDER BY s.name
"""
    ).format(sql.Identifier(config.SCHEMA), sql.Identifier(config.STATE_BOUNDARIES))
    try:
        with connect_rw() as conn:
            rows = conn.execute(query, {"aoi": aoi_geometry, "deg": config.BOUNDARY_TOLERANCE_M / 111_000}).fetchall()
    except psycopg.Error as e:
        raise HTTPException(500, f"Could not look up the states: {str(e).splitlines()[0]}")
    return [{"name": name, "folder": folder_for_boundary(name), "inside": inside} for name, inside in rows]
