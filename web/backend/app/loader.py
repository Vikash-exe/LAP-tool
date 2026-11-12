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
from psycopg.types.json import Jsonb

from . import cancel, config, statedata, typecast

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


def estimate(folder: str, names: list[str] | None = None) -> dict:
    """Files, rows and expected size in the database of a state's parquet files (footers only); only the named ones if given."""
    info = statedata.state_folders().get(folder)
    if info is None:
        raise HTTPException(404, f"No parcel files found for '{folder}'.")
    files = [f for f in info["files"] if names is None or f.name in names]
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
    size = sum(f.stat().st_size for f in files)
    return {
        "files": len(files),
        "rows": rows,
        "parquet_bytes": size,
        "est_bytes": int(size * config.DB_SIZE_RATIO),
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


def ensure_loaded(folder: str, progress=None, files: list[str] | None = None) -> str:
    """Make sure the parcels of this state are in the database; load them if not.

    `files` are the layers (parquet file names) that are needed; only those are loaded, and a state that is already
    loaded with some of them gets the missing ones added. Without `files`, all of the state's layers.
    Returns the table name. Blocks while another search is loading the same state.
    `progress(**info)` is called with state, status (waiting / loading / ready),
    files_done, files_total, rows_done, rows_total and current_file."""
    progress = progress or (lambda **_: None)
    wanted = list(dict.fromkeys(files)) if files else None
    lock = statedata.state_lock(folder)
    if not lock.acquire(blocking=False):
        progress(state=folder, status="waiting")
        while not lock.acquire(timeout=1):  # another search is loading this state: wait for it (Stop still works)
            cancel.check()
    try:
        with statedata.connect_rw() as conn:
            entry = statedata.get_entry(conn, folder)
            if entry and entry["status"] == statedata.READY:
                on_disk = statedata.source_files(folder)
                loaded = entry["loaded_files"]  # None: the table was loaded with every layer
                need = wanted if wanted is not None else on_disk
                missing = [] if loaded is None else [n for n in need if n not in loaded and n in on_disk]
                if not missing:
                    progress(state=folder, status="ready", files_done=entry["files_total"], files_total=entry["files_total"],
                             rows_done=entry["rows_total"], rows_total=entry["rows_total"])
                    return entry["table_name"]
                return _load(folder, progress, missing, entry)  # add the layers that are not in the table yet
            if entry:  # failed, or a half-loaded leftover: start clean
                statedata._drop(conn, folder, entry["table_name"])
        return _load(folder, progress, wanted)
    finally:
        lock.release()


def _load(folder: str, progress, names: list[str] | None = None, entry: dict | None = None) -> str:
    """Load the named files of a state (all of them without names) into its table. With `entry` (the registry row of a
    table that is ready) the files are added to that table; otherwise a new table is made."""
    info = statedata.state_folders().get(folder)
    if info is None:
        raise HTTPException(404, f"No parcel files found for '{folder}'.")
    files = [f for f in info["files"] if names is None or f.name in names]
    if not files:
        raise HTTPException(404, f"None of the chosen layers exist in the '{folder}' folder.")
    est = estimate(folder, [f.name for f in files])
    append = entry is not None

    # Refuse before touching the database if it would not fit in the storage budget.
    used = statedata.storage_in_use()
    budget = config.STORAGE_BUDGET_GB * 1e9
    if used + est["est_bytes"] > budget:
        loaded = ", ".join(e["state"] for e in statedata.registry()) or "none"
        raise HTTPException(
            409,
            f"Not enough room to load {folder}: it needs about {_gb(est['est_bytes'])} and the database already holds "
            f"{_gb(used)} of the {_gb(budget)} budget (loaded states: {loaded}). Clear a state first.",
        )

    table = entry["table_name"] if append else statedata.table_name(folder)
    base_files = list(entry["loaded_files"] or []) if append else []
    base_rows = int(entry["rows_done"] or 0) if append else 0
    reg = statedata.connect_rw()
    reg.autocommit = True
    conn = statedata.connect_rw()
    conn.autocommit = True
    done_rows = 0
    loaded_names = list(base_files)
    try:
        if append:
            _update(reg, folder, status=statedata.LOADING, files_total=len(base_files) + len(files), error=None,
                    rows_total=base_rows + est["rows"], est_bytes=(entry["est_bytes"] or 0) + est["est_bytes"])
        else:
            reg.execute(
                sql.SQL(
                    "INSERT INTO {} (state, table_name, status, files_total, files_done, rows_total, rows_done, est_bytes, error, loaded_files) "
                    "VALUES (%s, %s, %s, %s, 0, %s, 0, %s, NULL, %s) ON CONFLICT (state) DO UPDATE SET "
                    "table_name = EXCLUDED.table_name, status = EXCLUDED.status, files_total = EXCLUDED.files_total, "
                    "files_done = 0, rows_total = EXCLUDED.rows_total, rows_done = 0, est_bytes = EXCLUDED.est_bytes, "
                    "error = NULL, loaded_at = NULL, last_used = NULL, expires_at = NULL, loaded_files = EXCLUDED.loaded_files, "
                    "updated_at = now()"
                ).format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
                (folder, table, statedata.LOADING, len(files), est["rows"], est["est_bytes"], Jsonb([])),
            )
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))
            conn.execute(
                sql.SQL(
                    "CREATE TABLE {}.{} (id bigint GENERATED ALWAYS AS IDENTITY, state text, source text, attrs jsonb, "
                    "geometry geometry(Geometry, 4326))"
                ).format(sql.Identifier(config.SCHEMA), sql.Identifier(table))
            )

        total_files = len(base_files) + len(files)

        def report(files_done: int, current: str) -> None:
            progress(state=folder, status="loading", files_done=len(base_files) + files_done, files_total=total_files,
                     rows_done=done_rows, rows_total=est["rows"], current_file=current)

        report(0, files[0].name)
        saved_rows = 0
        for i, path in enumerate(files):
            for delta in _load_file(conn, table, folder, path):
                done_rows += delta
                report(i, path.name)
                # The registry (what the loaded-states panel shows) follows every ~500k rows, not only per file.
                if done_rows - saved_rows >= 500_000:
                    _update(reg, folder, rows_done=base_rows + done_rows)
                    saved_rows = done_rows
            loaded_names.append(path.name)
            _update(reg, folder, files_done=len(loaded_names), rows_done=base_rows + done_rows, loaded_files=Jsonb(loaded_names))
            report(i + 1, path.name)

        if not append:  # an added file goes into the index that is already there
            progress(state=folder, status="indexing", files_done=total_files, files_total=total_files,
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
                "UPDATE {} SET status = %s, files_done = %s, files_total = %s, rows_done = %s, rows_total = %s, loaded_at = now(), "
                "last_used = now(), expires_at = now() + make_interval(secs => %s), updated_at = now() WHERE state = %s"
            ).format(sql.Identifier(config.SCHEMA, config.STATE_REGISTRY)),
            (statedata.READY, len(loaded_names), len(loaded_names), base_rows + done_rows, base_rows + done_rows, config.STATE_TTL_SECONDS, folder),
        )
        progress(state=folder, status="ready", files_done=total_files, files_total=total_files,
                 rows_done=done_rows, rows_total=est["rows"])
        return table
    except BaseException as e:
        stopped = cancel.requested() or isinstance(e, cancel.Cancelled)
        message = e.detail if isinstance(e, HTTPException) else str(e).splitlines()[0] if str(e) else type(e).__name__
        if append:
            # The table is still good (a file is loaded in one transaction, so a half-loaded file left nothing behind):
            # it just keeps the layers it had.
            try:
                _update(reg, folder, status=statedata.READY, files_done=len(loaded_names), files_total=len(loaded_names),
                        rows_total=base_rows + done_rows, rows_done=base_rows + done_rows, error=None if stopped else str(message)[:500])
            except Exception:
                pass
        elif stopped:
            # Stopped by the user: remove the half-loaded table and its registry row completely.
            try:
                with statedata.connect_rw() as c2:
                    statedata._drop(c2, folder, table)
            except Exception:
                pass
        else:
            # Leave nothing half-loaded behind: drop the table, keep the registry row as 'failed' for the dashboard.
            try:
                conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))
                _update(reg, folder, status=statedata.FAILED, error=str(message)[:500])
            except Exception:
                pass
        if stopped:
            raise cancel.Cancelled()
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
