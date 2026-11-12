"""Copies parcel parquet files from the data folder into PostgreSQL (one table per state, cadastral.parcels_<state>).

This is the engine of src/sync_parcels.py, the one script that fills and updates the database. The web app never loads
anything: it only reads the tables. A sync is incremental and can be run again at any time:

* a file already in the catalog (cadastral.parcel_files) with the same size and modification time is skipped;
* a new file is loaded; a changed file has its old rows replaced; every file is one transaction, so an interrupted run
  leaves no half-loaded file and simply carries on where it stopped when run again;
* files are loaded in parallel (--jobs), the spatial index of a state is built once, after its files are in.

Each file is streamed in batches through COPY, with the type correction of typecast.py applied to the attributes.
"""

import io
import json
import math
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from . import config, footprint, statedata, typecast

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


# ---- one file ---------------------------------------------------------------------------------


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


# ---- the data folder ----------------------------------------------------------------------


def state_folders(root: Path | None = None) -> dict[str, list[Path]]:
    """{state folder name: its parquet files} for every state folder in the data folder."""
    root = root or config.MOUNT_ROOT
    out: dict[str, list[Path]] = {}
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name == "Village_boundaries":
                continue
            files = sorted(p for p in d.rglob("*.parquet") if not any(part.startswith(".") for part in p.relative_to(d).parts))
            if files:
                out[d.name] = files
    return out


def _layer_name(source: str) -> str:
    """The layer the dashboard shows: the source path without its state folder (what the SQL filters use too)."""
    return source.split("/", 1)[-1]


def _prepare_table(conn: psycopg.Connection, table: str) -> None:
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.{} (id bigint GENERATED ALWAYS AS IDENTITY, state text, source text, attrs jsonb, "
            "geometry geometry(Geometry, 4326))"
        ).format(sql.Identifier(config.SCHEMA), sql.Identifier(table))
    )
    # a changed file is replaced by its source name, so that has to be quick
    conn.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (source)").format(
            sql.Identifier(table + "_source_idx"), sql.Identifier(config.SCHEMA), sql.Identifier(table)
        )
    )


def _finish_table(conn: psycopg.Connection, table: str, say) -> None:
    say(f"  building the spatial index of {table} (once per table, can take a while for a big state)")
    conn.execute("SET maintenance_work_mem = '2GB'")
    conn.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} USING GIST (geometry)").format(
            sql.Identifier(table + "_geom_gix"), sql.Identifier(config.SCHEMA), sql.Identifier(table)
        )
    )
    conn.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, int(st.st_mtime)


def load_one(state: str, table: str, path_text: str) -> dict:
    """Worker (runs in its own process): replace this file's rows in the state table, then record it in the catalog."""
    path = Path(path_text)
    source = os.path.relpath(path, config.MOUNT_ROOT)
    size, mtime = _stamp(path)
    t0 = time.time()
    with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
        rows = 0
        with conn.transaction():
            conn.execute(sql.SQL("DELETE FROM {}.{} WHERE source = %s").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)), (source,))
            for n in _load_file(conn, table, state, path):
                rows += n
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (state, source, name, table_name, size_bytes, mtime, rows, loaded_at, footprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), NULL) ON CONFLICT (state, source) DO UPDATE SET "
                    "name = EXCLUDED.name, table_name = EXCLUDED.table_name, size_bytes = EXCLUDED.size_bytes, mtime = EXCLUDED.mtime, "
                    "rows = EXCLUDED.rows, loaded_at = now(), footprint = NULL"
                ).format(sql.Identifier(config.SCHEMA, config.CATALOG)),
                (state, source, _layer_name(source), table, size, mtime, rows),
            )
    return {"state": state, "source": source, "rows": rows, "seconds": time.time() - t0}


def footprint_one(state: str, path_text: str) -> dict:
    """Worker: where the file has parcels (for the layer map and for finding the layers that cover an area)."""
    path = Path(path_text)
    source = os.path.relpath(path, config.MOUNT_ROOT)
    t0 = time.time()
    data = footprint.compute(path)
    with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
        conn.execute(
            sql.SQL("UPDATE {} SET footprint = %s WHERE state = %s AND source = %s").format(sql.Identifier(config.SCHEMA, config.CATALOG)),
            (Jsonb(data), state, source),
        )
    return {"state": state, "source": source, "rows": data["rows"], "seconds": time.time() - t0}


# ---- the sync -----------------------------------------------------------------------------


def sync(states: list[str] | None = None, only: str | None = None, force: bool = False, prune: bool = False,
         jobs: int = 2, dry_run: bool = False, say=print) -> int:
    """Bring the database up to date with the data folder. Returns the number of files that failed."""
    folders = state_folders()
    if not folders:
        say(f"No state folders with parquet files in {config.MOUNT_ROOT} (set MOUNT_ROOT if the data is elsewhere).")
        return 1
    wanted = {s.lower() for s in states or []}
    unknown = [s for s in states or [] if s.lower() not in {f.lower() for f in folders}]
    if unknown:
        say(f"Unknown state folder(s): {', '.join(unknown)}. Folders: {', '.join(folders)}")
        return 1

    with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
        statedata.ensure_catalog(conn)
        rows = conn.execute(
            sql.SQL("SELECT state, source, size_bytes, mtime, footprint IS NOT NULL FROM {}").format(sql.Identifier(config.SCHEMA, config.CATALOG))
        ).fetchall()
    have = {(s, src): (size, mtime, fp) for s, src, size, mtime, fp in rows}

    plan: dict[str, dict] = {}
    for state, paths in folders.items():
        if wanted and state.lower() not in wanted:
            continue
        load, fill, same = [], [], 0
        current = set()
        for p in paths:
            if only and only.lower() not in p.name.lower():
                continue
            source = os.path.relpath(p, config.MOUNT_ROOT)
            current.add(source)
            known = have.get((state, source))
            if force or known is None or known[:2] != _stamp(p):
                load.append(p)
            elif not known[2]:
                fill.append(p)
            else:
                same += 1
        gone = [src for (s, src) in have if s == state and src not in current and not only]
        plan[state] = {"load": load, "fill": fill, "same": same, "gone": gone}

    say("Plan:")
    for state, p in plan.items():
        say(f"  {state}: {len(p['load'])} to load, {len(p['fill'])} extents to read, {p['same']} up to date"
            + (f", {len(p['gone'])} in the database but no longer in the folder" + (" (will be removed)" if prune else " (kept; use --prune to remove)") if p["gone"] else ""))
    if dry_run:
        for state, p in plan.items():
            for path in p["load"]:
                say(f"    would load {state}/{path.name}")
        return 0

    failed: list[str] = []
    ctx = multiprocessing.get_context("spawn")
    for state, p in plan.items():
        table = statedata.table_name(state)
        if p["load"]:
            say(f"\n{state}: loading {len(p['load'])} file(s) into cadastral.{table} with {max(1, jobs)} worker(s)")
            with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
                _prepare_table(conn, table)
            with ProcessPoolExecutor(max_workers=max(1, jobs), mp_context=ctx) as pool:
                futures = {pool.submit(load_one, state, table, str(path)): path for path in p["load"]}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        r = fut.result()
                        say(f"  loaded {path.name}: {r['rows']:,} parcels in {r['seconds']:.0f}s")
                        p["fill"].append(path)  # its extent is read next
                    except Exception as e:
                        failed.append(f"{state}/{path.name}")
                        say(f"  FAILED {path.name}: {str(e).splitlines()[0] if str(e) else type(e).__name__} (nothing of it was kept; run again to retry)")
            with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
                _finish_table(conn, table, say)
        loaded_ok = [x for x in p["fill"] if f"{state}/{x.name}" not in failed]
        if loaded_ok:
            say(f"{state}: reading where {len(loaded_ok)} file(s) have parcels (for the layer map)")
            with ProcessPoolExecutor(max_workers=max(1, jobs), mp_context=ctx) as pool:
                futures = {pool.submit(footprint_one, state, str(path)): path for path in loaded_ok}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        fut.result()
                        say(f"  extent of {path.name} done")
                    except Exception as e:
                        say(f"  extent of {path.name} failed: {str(e).splitlines()[0] if str(e) else type(e).__name__} (run again to retry)")
        if prune and p["gone"]:
            with psycopg.connect(**config.DB, connect_timeout=10, autocommit=True) as conn:
                for source in p["gone"]:
                    with conn.transaction():
                        conn.execute(sql.SQL("DELETE FROM {}.{} WHERE source = %s").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)), (source,))
                        conn.execute(sql.SQL("DELETE FROM {} WHERE state = %s AND source = %s").format(sql.Identifier(config.SCHEMA, config.CATALOG)), (state, source))
                    say(f"  removed {source} (no longer in the folder)")
                conn.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(config.SCHEMA), sql.Identifier(table)))

    say("\nDone." if not failed else f"\nDone, but {len(failed)} file(s) failed: {', '.join(failed)}")
    return len(failed)
