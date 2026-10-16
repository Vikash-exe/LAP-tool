#!/usr/bin/env python3
"""Load India_State_Boundary.zip into cadastral.state_boundaries (permanent, small).

The dashboard uses it to find out which state(s) an uploaded line or area falls in,
so that only those states' parcel data is loaded into the database.

    python load_state_boundaries.py

Safe to rerun: the table is rebuilt from the zip. The password comes from ~/.pgpass.
"""
import os
import subprocess
import sys
import zipfile

import psycopg

ZIP = "/home/geoint/projects/lap_tool/data/mount/India_State_Boundary.zip"
DB = dict(host="172.16.60.60", port=5432, dbname="Vikash", user="geouser")
SCHEMA = "cadastral"


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1])
    return proc.stdout


def main() -> None:
    with zipfile.ZipFile(ZIP) as zf:
        shp = next(n for n in zf.namelist() if n.lower().endswith(".shp"))
    vsi = f"/vsizip/{ZIP}/{shp}"
    conn_str = "PG:" + " ".join(f"{k}={v}" for k, v in DB.items())
    # Shapefile -> staging table: reprojected from the file's Lambert system to lon/lat, 2D, multipolygon.
    run([
        "ogr2ogr", "--config", "PG_USE_COPY", "YES", "-f", "PostgreSQL", conn_str, vsi,
        "-nln", f"{SCHEMA}.stg_state_boundaries", "-overwrite",
        "-t_srs", "EPSG:4326", "-dim", "XY", "-nlt", "PROMOTE_TO_MULTI",
        "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=ogc_fid", "-lco", "SPATIAL_INDEX=NONE", "-lco", "PRECISION=NO",
    ])
    with psycopg.connect(**DB, autocommit=True) as conn:
        with conn.transaction():
            conn.execute(f"DROP TABLE IF EXISTS {SCHEMA}.state_boundaries")
            conn.execute(
                f"""
CREATE TABLE {SCHEMA}.state_boundaries AS
SELECT row_number() OVER (ORDER BY state) AS id,
       btrim(state) AS name,
       state_lgd,
       CASE WHEN ST_IsValid(geom) THEN geom ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)) END::geometry(MultiPolygon, 4326) AS geometry
FROM {SCHEMA}.stg_state_boundaries
"""
            )
            conn.execute(f"CREATE INDEX state_boundaries_gix ON {SCHEMA}.state_boundaries USING GIST (geometry)")
        conn.execute(f"DROP TABLE IF EXISTS {SCHEMA}.stg_state_boundaries")
        n = conn.execute(f"SELECT count(*) FROM {SCHEMA}.state_boundaries").fetchone()[0]
        size = conn.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{SCHEMA}.state_boundaries'))").fetchone()[0]
    print(f"loaded {n} state boundaries ({size})")


if __name__ == "__main__":
    main()
