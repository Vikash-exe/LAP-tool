#!/usr/bin/env python3
"""Load the village boundary shapefiles (one zip per state) into cadastral.villages.

Each zip is read straight from the network mount by GDAL, reprojected to EPSG:4326
(the shapefiles are in projected systems and many are 3D), and loaded into a temporary
staging table; SQL then moves the rows into cadastral.villages with one consistent
set of attribute names. Every zip is one transaction and is logged in
cadastral.village_load_log, so a rerun skips finished files. The password comes
from ~/.pgpass.

    python load_villages.py --only GOA        # one file
    python load_villages.py                   # everything not yet loaded
    python load_villages.py --index           # (re)build the indexes at the end
"""
import argparse
import glob
import os
import subprocess
import sys
import time
import zipfile

import psycopg
from psycopg import sql

from pathlib import Path

LAP_HOME = Path(__file__).resolve().parents[1]  # the project root, wherever it is
MOUNT = Path(os.environ.get("MOUNT_ROOT", LAP_HOME / "data" / "mount"))
ROOT = str(MOUNT / "Village_boundaries")
DB = dict(host=os.environ.get("PGHOST", "172.16.60.60"), port=int(os.environ.get("PGPORT", "5432")), dbname=os.environ.get("PGDATABASE", "Vikash"), user=os.environ.get("PGUSER", "geouser"))
SCHEMA, TABLE, LOG, STAGING = "cadastral", "villages", "village_load_log", "stg_villages"

# Zip name (without .zip) -> the state label used for the parcel data, i.e. the
# name of that state's folder on the mount, so one state filter covers both
# layers. Spellings are the folders' own.
STATE_LABELS = {
    "ANDAMAN_&_NICOBAR_ISLANDS": "Andaman &  Nicobar",
    "ANDHRA_PRADESH": "Andhra Pradesh",
    "BIHAR": "Bihar",
    "CHANDIGARH": "Chandigarh",
    "CHHATTISGARH": "Chhattishgarh",
    "Dadra Nagar Haveli & Daman Diu": "Dadra & Nagar Haveli and Daman & Diu",
    "DELHI": "Delhi",
    "GOA": "Goa",
    "GUJARAT": "Gujarat",
    "HARYANA": "Haryana",
    "JHARKHAND": "jharkhand",
    "KARNATAKA": "Karnataka",
    "KERALA": "Kerala",
    "LAKSHYADWEEP": "Lakshadeep",
    "MADHYA_PRADESH": "Madhya Pradesh",
    "MAHARASHTRA": "Maharashtra",
    "ODISHA": "Odissa",
    "PUNDUCHERRY": "Pondicherry",
    "PUNJAB": "Punjab",
    "RAJASTHAN": "Rajasthan",
    "SIKKIM": "Sikkim",
    "TAMILNADU": "Tamil Nadu",
    "TELANGANA": "Telangana",
    "TRIPURA": "Tripura",
    "UTTARAKHAND": "Uttarakhand",
    "UTTAR_PRADESH": "Uttar Pradesh",
    "WEST_BENGAL": "West Bengal",
}

# Canonical attribute name -> shapefile columns that can hold it. The states
# spell these differently (Vill_Cat / Vill_cat, Sub_dist / Subdist, and
# "Villl_name" in Madhya Pradesh); GDAL lower-cases column names, so case
# differences are already gone by the time these are looked up.
ATTRIBUTES = {
    "village": ("vill_name", "villl_name"),
    "vill_lgd": ("vill_lgd",),
    "vill_category": ("vill_cat",),
    "sub_district": ("sub_dist", "subdist"),
    "subdis_lgd": ("subdis_lgd",),
    "subdis_type": ("subdis_typ",),
    "district": ("district",),
    "dist_lgd": ("dist_lgd",),
    "state_ut": ("state_ut", "state"),
    "state_lgd": ("state_lgd",),
    "remarks": ("remarks",),
}


def state_label(zip_path: str) -> str:
    stem = os.path.splitext(os.path.basename(zip_path))[0]
    return STATE_LABELS.get(stem, stem.replace("_", " ").title())


def shp_member(zip_path: str) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        shps = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    if not shps:
        raise RuntimeError("no .shp inside the zip")
    return shps[0]


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout).strip() else "command failed")
    return proc.stdout


def source_feature_count(vsi_path: str) -> int:
    for line in run(["ogrinfo", "-so", "-al", vsi_path]).splitlines():
        if line.startswith("Feature Count:"):
            return int(line.split(":")[1])
    raise RuntimeError("could not read the feature count")


def stage(zip_path: str) -> int:
    """GDAL: shapefile -> cadastral.stg_villages (EPSG:4326, 2D, multipolygon). Returns the source feature count."""
    vsi = f"/vsizip/{zip_path}/{shp_member(zip_path)}"
    expected = source_feature_count(vsi)
    conn_str = "PG:" + " ".join(f"{k}={v}" for k, v in DB.items())
    run([
        "ogr2ogr", "--config", "PG_USE_COPY", "YES",
        "-f", "PostgreSQL", conn_str, vsi,
        "-nln", f"{SCHEMA}.{STAGING}", "-overwrite",
        "-t_srs", "EPSG:4326", "-dim", "XY", "-nlt", "PROMOTE_TO_MULTI",
        "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=ogc_fid",
        "-lco", "SPATIAL_INDEX=NONE", "-lco", "PRECISION=NO",
    ])
    return expected


def attrs_expression(columns: set[str]) -> sql.Composable:
    """jsonb_build_object(...) over whichever source columns this staging table has."""
    pairs = []
    for key, candidates in ATTRIBUTES.items():
        present = [c for c in candidates if c in columns]
        if not present:
            continue
        values = sql.SQL(", ").join(sql.SQL("NULLIF(btrim(s.{}::text), '')").format(sql.Identifier(c)) for c in present)
        pairs.append(sql.SQL("{}, COALESCE({})").format(sql.Literal(key), values))
    return sql.SQL("jsonb_strip_nulls(jsonb_build_object({}))").format(sql.SQL(", ").join(pairs))


def setup(conn: psycopg.Connection) -> None:
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.{} (id bigint GENERATED ALWAYS AS IDENTITY, "
            "state text, source text, attrs jsonb, geometry geometry(MultiPolygon, 4326))"
        ).format(sql.Identifier(SCHEMA), sql.Identifier(TABLE))
    )
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.{} (source text PRIMARY KEY, n_rows bigint, seconds real, "
            "loaded_at timestamptz DEFAULT now())"
        ).format(sql.Identifier(SCHEMA), sql.Identifier(LOG))
    )


def load_zip(conn: psycopg.Connection, zip_path: str) -> None:
    name = os.path.basename(zip_path)
    state = state_label(zip_path)
    source = f"{state}/{name}"
    t0 = time.time()
    expected = stage(zip_path)
    columns = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (SCHEMA, STAGING),
        )
    }
    insert = sql.SQL(
        """
INSERT INTO {}.{} (state, source, attrs, geometry)
SELECT %s, %s, {},
       CASE WHEN ST_IsValid(s.geom) THEN s.geom
            ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(s.geom), 3)) END
FROM {}.{} s
WHERE s.geom IS NOT NULL AND NOT ST_IsEmpty(s.geom)
"""
    ).format(
        sql.Identifier(SCHEMA), sql.Identifier(TABLE), attrs_expression(columns), sql.Identifier(SCHEMA), sql.Identifier(STAGING)
    )
    with conn.transaction():
        staged = conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(STAGING))).fetchone()[0]
        if staged != expected:
            raise RuntimeError(f"staged {staged} rows, the shapefile has {expected}")
        cur = conn.execute(insert, (state, source))
        loaded = cur.rowcount
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.{} (source, n_rows, seconds) VALUES (%s, %s, %s) "
                "ON CONFLICT (source) DO UPDATE SET n_rows = EXCLUDED.n_rows, seconds = EXCLUDED.seconds, loaded_at = now()"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(LOG)),
            (source, loaded, time.time() - t0),
        )
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(STAGING)))
    note = "" if loaded == expected else f"  (!) {expected - loaded} features had no usable geometry and were skipped"
    print(f"  done {name} -> {state}: {loaded:,} villages in {time.time() - t0:.0f}s{note}", flush=True)


def build_indexes(conn: psycopg.Connection) -> None:
    t0 = time.time()
    conn.execute("SET maintenance_work_mem = '2GB'")
    print("building indexes ...", flush=True)
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS villages_geom_gix ON {}.{} USING GIST (geometry)").format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)))
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS villages_state_idx ON {}.{} (state)").format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)))
    conn.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)))
    print(f"indexes done in {time.time() - t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="only zips whose name contains this text, e.g. GOA")
    ap.add_argument("--index", action="store_true", help="build the spatial and state indexes at the end")
    args = ap.parse_args()

    zips = sorted(glob.glob(os.path.join(ROOT, "*.zip")))
    if args.only:
        zips = [z for z in zips if args.only.lower() in os.path.basename(z).lower()]
    print(f"{len(zips)} zip files selected", flush=True)

    with psycopg.connect(**DB, autocommit=True) as conn:
        setup(conn)
        finished = {r[0] for r in conn.execute(sql.SQL("SELECT source FROM {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(LOG)))}
        for z in zips:
            source = f"{state_label(z)}/{os.path.basename(z)}"
            if source in finished:
                print("skip (already loaded)", os.path.basename(z), flush=True)
                continue
            print("loading", os.path.basename(z), flush=True)
            try:
                load_zip(conn, z)
            except Exception as e:  # keep going: one bad file should not stop the rest
                print(f"  FAILED {os.path.basename(z)}: {e}", file=sys.stderr, flush=True)
        if args.index:
            build_indexes(conn)


if __name__ == "__main__":
    main()
