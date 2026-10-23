"""Loads one state's parcel parquet files into its own table, cadastral.parcels_<state>.

Used by the dashboard (a search that touches a state not yet in the database starts this)
and by src/load_cadastral.py. Each file is streamed in batches through COPY, one
transaction per file, with the type correction of typecast.py applied to the attributes.
Progress is reported through a callback so the dashboard can show it.
"""

import io
import json
import math
import os
import time

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import psycopg
from fastapi import HTTPException
from psycopg import sql

from . import config, statedata, typecast

SRID = 4326
GEOM = "geometry"
SKIP_COLS = {"bbox", GEOM}


# ---- row conversion --------------------------------------------------------------------


def _get_srid(schema: pa.Schema) -> int:
    try:
        geo = json.loads(schema.metadata[b"geo"])
        crs = geo["columns"][GEOM].get("crs")
        return 4326 if crs is None else int(crs["id"]["code"])
    except Exception:
        return 4326


def _to_ewkb_hex(b: bytes | None) -> str | None:
    """Plain 2D WKB -> EWKB hex carrying the SRID, so COPY can fill a geometry(Geometry,4326) column."""
    if b is None:
        return None
    order = "little" if b[0] == 1 else "big"
    t = int.from_bytes(b[1:5], order)
    if t > 7:
        raise ValueError(f"unsupported WKB type code {t} (Z/M or already EWKB)")
    t |= 0x20000000
    return (b[:1] + t.to_bytes(4, order) + SRID.to_bytes(4, order) + b[5:]).hex()


def _clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace("\x00", "")
        return v if v != "" else None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _attrs_json(batch: pa.RecordBatch, names: list[str], source: str) -> list[str]:
    """One JSON text per row; `source` selects which string columns the type correction turns into numbers."""
    cols = [batch.column(n).to_pylist() for n in names]
    out = []
    for vals in zip(*cols):
        d = {}
        for n, v in zip(names, vals):
            v = _clean(v)
            if v is not None:
                d[n] = v
        out.append(json.dumps(typecast.apply(source, d), ensure_ascii=False, separators=(",", ":"), default=str))
    return out


# ---- sizing -----------------------------------------------------------------------------


def estimate(folder: str) -> dict:
    """Files, rows and expected size in the database of a state's parquet files (footers only)."""
    info = statedata.state_folders().get(folder)
    if info is None:
        raise HTTPException(404, f"No parcel files found for '{folder}'.")
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in info["files"])
    return {
        "files": len(info["files"]),
        "rows": rows,
        "parquet_bytes": info["bytes"],
        "est_bytes": int(info["bytes"] * config.DB_SIZE_RATIO),
    }


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


# ---- registry updates (own autocommit connection, so they are visible during a long COPY) ----


def _update(reg: psycopg.Connection, folder: str, **fields) -> None:
    sets = [sql.SQL("{} = %s").format(sql.Identifier(k)) for k in fields] + [sql.SQL("updated_at = now()")]
    reg.execute(
        sql.SQL("UPDATE {} SET {} WHERE state = %s").format(
            sql.Identifier(config.SCHEMA, config.STATE_REGISTRY), sql.SQL(", ").join(sets)
        ),
        [*fields.values(), folder],
    )


# ---- loading ----------------------------------------------------------------------------


def ensure_loaded(folder: str, progress=None) -> str:
    """Make sure the parcels of this state are in the database; load them if not.

    Returns the table name. Blocks while another search is loading the same state.
    `progress(**info)` is called with state, status (waiting / loading / ready),
    files_done, files_total, rows_done, rows_total and current_file."""
    progress = progress or (lambda **_: None)
    lock = statedata.state_lock(folder)
    if not lock.acquire(blocking=False):
        progress(state=folder, status="waiting")
        lock.acquire()  # another search is loading this state: wait for it
    try:
        with statedata.connect_rw() as conn:
            entry = statedata.get_entry(conn, folder)
            if entry and entry["status"] == statedata.READY:
                progress(state=folder, status="ready", files_done=entry["files_total"], files_total=entry["files_total"],
                         rows_done=entry["rows_total"], rows_total=entry["rows_total"])
                return entry["table_name"]
            if entry:  # failed, or a half-loaded leftover: start clean
                statedata._drop(conn, folder, entry["table_name"])
        return _load(folder, progress)
    finally:
        lock.release()


def _load(folder: str, progress) -> str:
    info = statedata.state_folders().get(folder)
    if info is None:
        raise HTTPException(404, f"No parcel files found for '{folder}'.")
    est = estimate(folder)

    # Refuse before touching the database if the state would not fit in the storage budget.
    used = statedata.storage_in_use()
    budget = config.STORAGE_BUDGET_GB * 1e9
    if used + est["est_bytes"] > budget:
        loaded = ", ".join(e["state"] for e in statedata.registry()) or "none"
        raise HTTPException(
            409,
            f"Not enough room to load {folder}: it needs about {_gb(est['est_bytes'])} and the database already holds "
            f"{_gb(used)} of the {_gb(budget)} budget (loaded states: {loaded}). Clear a state first.",
        )

    table = statedata.table_name(folder)
    files = info["files"]
    reg = statedata.connect_rw()
    reg.autocommit = True
    conn = statedata.connect_rw()
    conn.autocommit = True
    done_rows = 0
    try:
        reg.execute(
            sql.SQL(
                "INSERT INTO {} (state, table_name, status, files_total, files_done, rows_total, rows_done, est_bytes, error) "
                "VALUES (%s, %s, %s, %s, 0, %s, 0, %s, NULL) ON CONFLICT (state) DO UPDATE SET "
                "table_name = EXCLUDED.table_name, status = EXCLUDED.status, files_total = EXCLUDED.files_total, "
                "files_done = 0, rows_total = EXCLUDED.rows_total, rows_done = 0, est_bytes = EXCLUDED.est_bytes, "
                "error = NULL, loaded_at = NULL, last_used = NULL, expires_at = NULL, updated_at = now()"
            ).format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
            (folder, table, statedata.LOADING, len(files), est["rows"], est["est_bytes"]),
        )
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))
        conn.execute(
            sql.SQL(
                "CREATE TABLE {}.{} (id bigint GENERATED ALWAYS AS IDENTITY, state text, source text, attrs jsonb, "
                "geometry geometry(Geometry, 4326))"
            ).format(sql.Identifier(config.SCHEMA), sql.Identifier(table))
        )

        def report(files_done: int, current: str) -> None:
            progress(state=folder, status="loading", files_done=files_done, files_total=len(files),
                     rows_done=done_rows, rows_total=est["rows"], current_file=current)

        report(0, files[0].name)
        saved_rows = 0
        for i, path in enumerate(files):
            for delta in _load_file(conn, table, folder, path):
                done_rows += delta
                report(i, path.name)
                # The registry (what the loaded-states panel shows) follows every ~500k rows, not only per file.
                if done_rows - saved_rows >= 500_000:
                    _update(reg, folder, rows_done=done_rows)
                    saved_rows = done_rows
            _update(reg, folder, files_done=i + 1, rows_done=done_rows)
            report(i + 1, path.name)

        progress(state=folder, status="indexing", files_done=len(files), files_total=len(files),
                 rows_done=done_rows, rows_total=est["rows"])
        conn.execute("SET maintenance_work_mem = '2GB'")
        conn.execute(
            sql.SQL("CREATE INDEX {} ON {}.{} USING GIST (geometry)").format(
                sql.Identifier(table + "_geom_gix"), sql.Identifier(config.SCHEMA), sql.Identifier(table)
            )
        )
        conn.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))
        reg.execute(
            sql.SQL(
                "UPDATE {} SET status = %s, files_done = %s, rows_done = %s, loaded_at = now(), last_used = now(), "
                "expires_at = now() + make_interval(secs => %s), updated_at = now() WHERE state = %s"
            ).format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
            (statedata.READY, len(files), done_rows, config.STATE_TTL_SECONDS, folder),
        )
        progress(state=folder, status="ready", files_done=len(files), files_total=len(files),
                 rows_done=done_rows, rows_total=est["rows"])
        return table
    except BaseException as e:
        # Leave nothing half-loaded behind: drop the table, keep the registry row as 'failed' for the dashboard.
        message = e.detail if isinstance(e, HTTPException) else str(e).splitlines()[0] if str(e) else type(e).__name__
        try:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))
            _update(reg, folder, status=statedata.FAILED, error=str(message)[:500])
        except Exception:
            pass
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(500, f"Loading {folder} failed: {message}")
    finally:
        reg.close()
        conn.close()


def _load_file(conn: psycopg.Connection, table: str, folder: str, path):
    """Streams one parquet file into the table in one transaction; yields the rows copied after each batch."""
    rel = os.path.relpath(path, config.MOUNT_ROOT)
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    if _get_srid(schema) != SRID:
        raise RuntimeError(f"{rel}: CRS is not EPSG:{SRID}")
    attr_names = [n for n in schema.names if n not in SKIP_COLS]
    total = pf.metadata.num_rows
    copy_sql = sql.SQL("COPY {}.{} (state, source, attrs, geometry) FROM STDIN WITH (FORMAT csv)").format(
        sql.Identifier(config.SCHEMA), sql.Identifier(table)
    )
    with conn.transaction():
        with conn.cursor() as cur:
            with cur.copy(copy_sql) as cp:
                for batch in pf.iter_batches(batch_size=config.LOAD_BATCH_ROWS, columns=attr_names + [GEOM]):
                    n = batch.num_rows
                    tbl = pa.table(
                        {
                            "state": pa.array([folder] * n, pa.string()),
                            "source": pa.array([rel] * n, pa.string()),
                            "attrs": pa.array(_attrs_json(batch, attr_names, rel), pa.string()),
                            "geometry": pa.array([_to_ewkb_hex(v) for v in batch.column(GEOM).to_pylist()], pa.string()),
                        }
                    )
                    buf = io.BytesIO()
                    pacsv.write_csv(tbl, buf, pacsv.WriteOptions(include_header=False))
                    cp.write(buf.getvalue())
                    yield n
            if cur.rowcount != total:
                raise RuntimeError(f"{rel}: copied {cur.rowcount} rows, expected {total}")
