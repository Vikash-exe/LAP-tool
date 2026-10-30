"""Shared settings for the cadastral parcel finder backend.

The database password is never stored here: psycopg/libpq reads it from the
PGPASSWORD environment variable or from ~/.pgpass.
"""

import os
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = WEB_ROOT / "data" / "results"

DB = dict(
    host=os.environ.get("PGHOST", "172.16.60.60"),
    port=int(os.environ.get("PGPORT", "5432")),
    dbname=os.environ.get("PGDATABASE", "Vikash"),
    user=os.environ.get("PGUSER", "geouser"),
)
SCHEMA = "cadastral"

# Parcel data is NOT kept permanently in the database (it would not fit): a state's parcel
# files are loaded into their own table, cadastral.parcels_<state>, when a search touches
# that state, and the table is dropped again STATE_TTL_SECONDS after it was last used or when
# the user clears it in the dashboard. cadastral.state_tables tracks what is loaded.
STATE_REGISTRY = "state_tables"
STATE_BOUNDARIES = "state_boundaries"  # permanent; tells which state(s) an area falls in
MOUNT_ROOT = Path(os.environ.get("MOUNT_ROOT", WEB_ROOT.parent / "data" / "mount"))
STATE_TTL_SECONDS = int(os.environ.get("STATE_TTL_SECONDS", str(3 * 3600)))
SWEEP_INTERVAL_SECONDS = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "60"))
# Most the loaded parcel tables may take together (plus the permanent village and boundary
# tables). SQL cannot see the server's free disk, so this budget is what protects it.
STORAGE_BUDGET_GB = float(os.environ.get("STORAGE_BUDGET_GB", "200"))
# Measured on Goa: bytes in the database per byte of parquet (2.4 GB for 0.63 GB).
DB_SIZE_RATIO = 3.8
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
