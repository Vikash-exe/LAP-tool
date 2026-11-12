"""Shared settings for the cadastral parcel finder backend.

The database password is never stored here: psycopg/libpq reads it from the
PGPASSWORD environment variable or from ~/.pgpass.
"""

import os
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = WEB_ROOT / "data" / "results"

# The database comes from the environment (PGHOST, PGPORT, PGDATABASE, PGUSER; the password from PGPASSWORD or ~/.pgpass).
# A setting that is not given is left to libpq's own default (host here defaults to this machine).
DB = {
    k: v
    for k, v in dict(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE"),
        user=os.environ.get("PGUSER"),
    ).items()
    if v
}
SCHEMA = "cadastral"

# All parcel data lives permanently in PostgreSQL: one table per state, cadastral.parcels_<state>, filled and kept up
# to date by src/sync_parcels.py from the data folder (MOUNT_ROOT). The web app only reads them.
# cadastral.parcel_files is the catalog: one row per source file that was loaded (see statedata.py).
CATALOG = "parcel_files"
STATE_BOUNDARIES = "state_boundaries"  # tells which state(s) an area falls in
# The folder of state folders with the parcel parquet files. Only the sync script reads it.
MOUNT_ROOT = Path(os.environ.get("MOUNT_ROOT", WEB_ROOT.parent / "data" / "mount"))
# A state whose boundary lies within this distance of the uploaded area counts as touched
# (coastal lines and borders are not exactly where the boundary file puts them).
BOUNDARY_TOLERANCE_M = 1000
LOAD_BATCH_ROWS = 100_000
# Village boundaries (loaded by src/load_villages.py; permanent); same column layout as the parcels.
VILLAGES_TABLE = "villages"

# The web app only ever reads, so the session is read-only and bounded in time.
DB_SESSION_OPTIONS = "-c statement_timeout=300000 -c default_transaction_read_only=on"

# One query may return at most this many parcels; beyond that the user is asked
# to narrow the area or pick states (a browser cannot usefully draw more anyway).
MAX_FEATURES = int(os.environ.get("MAX_FEATURES", "100000"))
# Village polygons are far heavier than parcels (thousands of vertices each), so they get a lower cap.
MAX_VILLAGES = int(os.environ.get("MAX_VILLAGES", "20000"))

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
MAX_AOI_VERTICES = 1_000_000
RESULT_TTL_HOURS = 24

# Primary vector files a user may upload (directly, or inside a .zip/.kmz).
AOI_SUFFIXES = (".shp", ".kml", ".geojson", ".json", ".gpkg", ".gml")
ARCHIVE_SUFFIXES = (".zip", ".kmz")
# Sidecar files that travel with a loose shapefile upload.
SIDECAR_SUFFIXES = (".dbf", ".shx", ".prj", ".cpg", ".qpj", ".sbn", ".sbx")

# "both": the parcels that intersect the area, each tagged fully_inside (or not), so the page can show the two sets as separate layers
QUERY_MODES = ("intersects", "within", "crossing", "both")

# Which attribute column holds the parcel number, per source file (file name
# only, without the state folder; the names are case-sensitive and exactly as
# stored). The "parcel table" CSV reads its `parcel number` column from here.
# A source missing from this list falls back to the first of
# PARCEL_NUMBER_FALLBACKS that it has, so newly loaded states still get a value.
PARCEL_NUMBER_COLUMNS = {
    "Goa_Bhunaksha_Cadastrals.parquet": "Plot",
    "NBSS_Goa_Cadastrals.parquet": "Survey_No",
    "Onemapgoa_GA_Cadastrals.parquet": "plot",
    "Onemapgoa_GA_Cadastrals_Mar2026.parquet": "plot",
    "GSDL_Cadastrals.parquet": "M_KHASRA_N",  # Delhi: the khasra number
}
PARCEL_NUMBER_FALLBACKS = ("plot", "survey_no", "survey", "khasra", "parcel_num", "kide")

# The parcel tables' village name, used only when the village boundary under a parcel has no name
# (e.g. 315 of Delhi's 369 village boundaries are unnamed urban areas). Per source file, same idea as
# above. A source missing from the list falls back to the first of PARCEL_VILLAGE_FALLBACKS it has;
# these are names that hold a village NAME (not a code), compared case-insensitively.
PARCEL_VILLAGE_COLUMNS = {
    "GSDL_Cadastrals.parquet": "VILL_NM",  # Delhi
    "Goa_Bhunaksha_Cadastrals.parquet": "VillageNam",
    "Onemapgoa_GA_Cadastrals.parquet": "villagenam",
    "Onemapgoa_GA_Cadastrals_Mar2026.parquet": "villagenam",
}
PARCEL_VILLAGE_FALLBACKS = ("vill_nm", "villagenam", "village_nam", "village_name", "villagename", "vill_name")
