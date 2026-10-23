"""Which states' parcel data is in the database, and for how long.

Parcel tables are loaded on demand (see loader.py) and dropped again after
STATE_TTL_SECONDS without use or when the user clears them. This module holds the
bookkeeping: finding the states an area falls in, the registry of loaded states
(cadastral.state_tables), dropping tables, and the sweep that drops expired ones.
"""

import re
import threading
import time
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import config

LOADING, READY, FAILED = "loading", "ready", "failed"

# Boundary-file name (normalised) -> parcel folder name (normalised), where the two
# spell the same state differently.
_ALIASES = {
    "odisha": "odissa",
    "puducherry": "pondicherry",
    "lakshadweep": "lakshadeep",
    "chhattisgarh": "chhattishgarh",
    "jammuandkashmir": "jandk",  # the folder is "J&K"
}


def connect_rw() -> psycopg.Connection:
    """A connection that may create and drop tables (the search connection is read-only)."""
    return psycopg.connect(**config.DB, connect_timeout=10)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def table_name(folder: str) -> str:
    """cadastral.<this> holds the parcels of the state folder."""
    return "parcels_" + (re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", folder).encode("ascii", "ignore").decode().lower()).strip("_") or "state")


# ---- the state folders on the mount -------------------------------------------------

_folders_cache: tuple[float, dict] | None = None


def state_folders() -> dict[str, dict]:
    """{folder name: {"files": [Path, ...], "bytes": parquet bytes}} for every state folder on the mount."""
    global _folders_cache
    if _folders_cache and time.time() - _folders_cache[0] < 60:
        return _folders_cache[1]
    folders: dict[str, dict] = {}
    if config.MOUNT_ROOT.is_dir():
        for d in sorted(config.MOUNT_ROOT.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name == "Village_boundaries":
                continue
            files = sorted(d.rglob("*.parquet"))
            if files:
                folders[d.name] = {"files": files, "bytes": sum(f.stat().st_size for f in files)}
    _folders_cache = (time.time(), folders)
    return folders


def source_files(folder: str) -> list[str]:
    """Parquet file names of a state folder (the 'source' names its parcels carry)."""
    return [f.name for f in state_folders().get(folder, {}).get("files", [])]


def mount_available() -> bool:
    """True when the parcel data folder holds state folders (it may be unmounted or empty on a new server)."""
    return bool(state_folders())


def _ready_states() -> list[str]:
    """States whose parcel table is in the database now. They can be searched even where the
    parquet files themselves are not available (nothing needs to be loaded for them)."""
    try:
        with connect_rw() as conn:
            rows = conn.execute(
                sql.SQL("SELECT state FROM {} WHERE status = %s").format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
                (READY,),
            ).fetchall()
    except psycopg.Error:
        return []
    return [r[0] for r in rows]


def folder_for_boundary(name: str) -> str | None:
    """The parcel state matching a state name from the boundary file, or None when that state has no parcel data:
    either it has a folder of parquet files, or its table is already loaded in the database."""
    key = _norm(name)
    key = _ALIASES.get(key, key)
    for folder in list(state_folders()) + _ready_states():
        if _norm(folder) == key:
            return folder
    return None


# ---- which states an area touches ----------------------------------------------------


def detect_states(aoi_geometry: str) -> list[dict]:
    """The states whose boundary the area intersects or lies within BOUNDARY_TOLERANCE_M of.

    Returns [{"name": boundary name, "folder": parcel folder or None, "inside": bool}]."""
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


# ---- the registry of loaded states ---------------------------------------------------

_REGISTRY_COLUMNS = (
    "state, table_name, status, files_total, files_done, rows_total, rows_done, est_bytes, "
    "loaded_at, last_used, expires_at, error"
)


def _entry(row: tuple) -> dict:
    keys = [c.strip() for c in _REGISTRY_COLUMNS.split(",")]
    return dict(zip(keys, row))


def registry() -> list[dict]:
    """Every state with a table (loaded or loading), with its live size and seconds until it expires."""
    query = sql.SQL(
        "SELECT {cols}, COALESCE(pg_total_relation_size(to_regclass(quote_ident(%(schema)s::text) || '.' || quote_ident(table_name))), 0), "
        "EXTRACT(EPOCH FROM (expires_at - now())) FROM {reg} ORDER BY state"
    ).format(cols=sql.SQL(_REGISTRY_COLUMNS), reg=sql.Identifier(config.SCHEMA, config.STATE_REGISTRY))
    with connect_rw() as conn:
        rows = conn.execute(query, {"schema": config.SCHEMA}).fetchall()
    out = []
    for row in rows:
        e = _entry(row[:12])
        e["size_bytes"] = int(row[12])
        e["expires_in_seconds"] = None if row[13] is None else int(row[13])
        for k in ("loaded_at", "last_used", "expires_at"):
            e[k] = e[k].isoformat() if e[k] else None
        out.append(e)
    return out


def get_entry(conn: psycopg.Connection, folder: str) -> dict | None:
    row = conn.execute(
        sql.SQL("SELECT {cols} FROM {reg} WHERE state = %s").format(
            cols=sql.SQL(_REGISTRY_COLUMNS), reg=sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)
        ),
        (folder,),
    ).fetchone()
    return _entry(row) if row else None


def ready_tables(folders: list[str]) -> dict[str, str]:
    """{folder: table name} for those of the given states that are loaded and ready."""
    if not folders:
        return {}
    with connect_rw() as conn:
        rows = conn.execute(
            sql.SQL("SELECT state, table_name FROM {} WHERE status = %s AND state = ANY(%s)").format(
                sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)
            ),
            (READY, folders),
        ).fetchall()
    return dict(rows)


def touch(folders: list[str]) -> None:
    """Mark the states as used now: their tables live another STATE_TTL_SECONDS."""
    if not folders:
        return
    with connect_rw() as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {} SET last_used = now(), expires_at = now() + make_interval(secs => %s), updated_at = now() "
                "WHERE status = %s AND state = ANY(%s)"
            ).format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
            (config.STATE_TTL_SECONDS, READY, folders),
        )


def storage_in_use() -> int:
    """Bytes the loaded parcel tables take, a loading table counted at its estimate, plus the permanent tables."""
    used = 0
    for e in registry():
        used += e["size_bytes"] if e["status"] == READY else (e["est_bytes"] or 0)
    with connect_rw() as conn:
        for t in (config.VILLAGES_TABLE, config.STATE_BOUNDARIES):
            row = conn.execute("SELECT pg_total_relation_size(to_regclass(%s))", (f"{config.SCHEMA}.{t}",)).fetchone()
            used += row[0] or 0
    return used


# ---- per-state locks and "in use" tracking -------------------------------------------

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_guard = threading.Lock()
_in_use: dict[str, int] = defaultdict(int)  # folder -> number of running searches using it


def state_lock(folder: str) -> threading.Lock:
    """One lock per state: a load of that state, or a drop, never runs at the same time as another."""
    return _locks[folder]


@contextmanager
def using(folders: list[str]):
    """While a search is using these states, the sweep and manual drops leave them alone."""
    with _guard:
        for f in folders:
            _in_use[f] += 1
    try:
        yield
    finally:
        with _guard:
            for f in folders:
                _in_use[f] -= 1


def _is_in_use(folder: str) -> bool:
    with _guard:
        return _in_use[folder] > 0


# ---- dropping ---------------------------------------------------------------------------


def _drop(conn: psycopg.Connection, folder: str, table: str) -> None:
    with conn.transaction():
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))
        conn.execute(sql.SQL("DELETE FROM {} WHERE state = %s").format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)), (folder,))


def drop_state(folder: str) -> dict:
    """Drop one state's whole parcel table (the manual 'clear' button). Refuses while a search is using it."""
    if _is_in_use(folder):
        raise HTTPException(409, f"{folder} is being used by a search that is still running. Try again when it has finished.")
    if not state_lock(folder).acquire(blocking=False):
        raise HTTPException(409, f"{folder} is being loaded right now.")
    try:
        with connect_rw() as conn:
            entry = get_entry(conn, folder)
            if entry is None:
                raise HTTPException(404, f"No parcel table is loaded for {folder}.")
            size = conn.execute(
                "SELECT COALESCE(pg_total_relation_size(to_regclass(%s)), 0)", (f"{config.SCHEMA}.{entry['table_name']}",)
            ).fetchone()[0]
            _drop(conn, folder, entry["table_name"])
        return {"state": folder, "freed_bytes": int(size)}
    finally:
        state_lock(folder).release()


def drop_all() -> dict:
    """Drop every loaded state table that is not in use; report the ones that were skipped."""
    dropped, skipped = [], []
    for e in registry():
        try:
            dropped.append(drop_state(e["state"]))
        except HTTPException as exc:
            skipped.append({"state": e["state"], "reason": exc.detail})
    return {"dropped": dropped, "skipped": skipped, "freed_bytes": sum(d["freed_bytes"] for d in dropped)}


def sweep_expired() -> list[str]:
    """Drop the tables whose time is up, and any half-loaded or failed leftovers. Returns the states dropped."""
    dropped: list[str] = []
    with connect_rw() as conn:
        rows = conn.execute(
            sql.SQL(
                "SELECT state, table_name, status FROM {} WHERE "
                "(status = %s AND expires_at < now()) OR "
                "(status = %s AND updated_at < now() - interval '10 minutes') OR "
                "(status = %s AND updated_at < now() - interval '2 minutes')"
            ).format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
            (READY, FAILED, LOADING),
        ).fetchall()
    for folder, table, status in rows:
        if _is_in_use(folder):
            continue
        # A 'loading' row that this process is not loading is an orphan (the backend was stopped mid-load).
        lock = state_lock(folder)
        if not lock.acquire(blocking=False):
            continue
        try:
            with connect_rw() as conn:
                _drop(conn, folder, table)
            dropped.append(folder)
        finally:
            lock.release()
    return dropped


_sweeper_started = False


def start_sweeper() -> None:
    """Background thread: drops expired state tables every SWEEP_INTERVAL_SECONDS (and once right away)."""
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    def loop() -> None:
        while True:
            try:
                dropped = sweep_expired()
                if dropped:
                    print(f"[sweep] dropped expired state tables: {', '.join(dropped)}", flush=True)
            except Exception as e:  # the sweeper must never die
                print(f"[sweep] error: {e}", flush=True)
            time.sleep(config.SWEEP_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="state-sweeper", daemon=True).start()
