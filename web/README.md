# Cadastral Parcel Finder

Upload an area of interest (KML, KMZ, Shapefile, GeoJSON, GeoPackage) and get every
cadastral parcel that intersects it, shown on a basemap and downloadable as GeoJSON, GeoPackage,
Shapefile, KML or CSV. The parcel data of a state is loaded into PostGIS only when an area falls in
that state, and dropped again afterwards (see *Parcel data is loaded on demand*).

Same stack as `land_map_merge/web`: FastAPI backend + Vite/React/TypeScript/MapLibre frontend.

```
web/
  backend/app/   config.py  aoi.py  query.py  exports.py  main.py
  frontend/src/  App.tsx  MapView.tsx  api.ts  basemaps.ts  ProgressBar.tsx  Resizer.tsx
  data/results/  per-search result files (auto-deleted after 24 h)
```

## Setting up on a new machine

`LAP` below stands for the folder the project lives in (for example `/home/cigma/tools/LAP`); nothing in the
code depends on where it is.

1. `LAP/setup.sh` creates the Python environment `LAP/lapenv` from `requirements-lock.txt` (the exact versions
   the project was built with; needs Python 3.13 with venv, and GDAL's `ogr2ogr`/`ogrinfo` installed).
2. Database access: the app reads the connection from `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER`
   (defaults `172.16.60.60` / `5432` / `Vikash` / `geouser`) and the password from `~/.pgpass`:
   `echo '172.16.60.60:5432:Vikash:geouser:THE_PASSWORD' >> ~/.pgpass && chmod 600 ~/.pgpass`
3. The parcel data (the state folders of parquet files, `Village_boundaries/`, `India_State_Boundary.zip`) is read
   from `LAP/data/mount`, or from wherever `MOUNT_ROOT` points. On the server it is the network share
   `\\145.145.53.17\Survey_data\Cadastral_Map_India\cadastral_data`, mounted read-only. Run
   `sudo LAP/mount_share.sh` once (it needs `~/.smbcreds` with `username=` and `password=` lines, mode 600): it adds the
   `/etc/fstab` entry, so the share is mounted again after every reboot (`nofail`: a missing file server never blocks
   booting), mounts it, and checks that it is the right folder and not the whole company share.
4. Start it: `LAP/web/run_backend.sh` serves the API and the built frontend on `http://127.0.0.1:8010`
   (`HOST` / `PORT` change that). After changing frontend code run `LAP/web/build_frontend.sh`.

The permanent tables (village boundaries, state boundaries) are loaded once per database with
`LAP/src/load_villages.py` and `LAP/src/load_state_boundaries.py`; they are already in the `Vikash` database.

## Run it

```bash
LAP/web/run_backend.sh        # API and the built frontend, one process, http://127.0.0.1:8010
LAP/web/status.sh             # is it running? (tells this project's backend apart from other uvicorns)
```

The backend listens on the server itself only. To open it from your own PC, tunnel the port, e.g.
`ssh -N -L 18010:127.0.0.1:8010 cigma@172.16.60.60` and browse to `http://localhost:18010` (or forward the port in
VS Code). To make it reachable from other machines start it with `HOST=0.0.0.0`; note the dashboard has no login.

Only while changing the frontend: `LAP/web/run_frontend.sh` starts the dev server on 5174 (proxying /api to the
backend), and `LAP/web/build_frontend.sh` rebuilds the version the backend serves.

First time only: `LAP/setup.sh`, see *Setting up on a new machine*.

## Running it as a service (systemd) and watching it in the terminal

The backend runs as a systemd **user** service, `lap-backend`, so it no longer lives in a tmux pane: it restarts by
itself 5 seconds after any crash (`Restart=always`), starts again after a reboot, and survives SSH logouts
(`loginctl enable-linger cigma` is on). The unit file is `~/.config/systemd/user/lap-backend.service`; it runs
`web/run_backend.sh` (so `HOST` / `PORT` still apply, set them with `systemctl --user edit lap-backend`:
`[Service]` then `Environment=PORT=8010`).

```bash
# is it running? (no sudo needed for any of these)
systemctl --user status lap-backend          # running or failed, pid, memory, last log lines
LAP/web/status.sh                            # same question plus a /api/health check, and other uvicorns on the box
curl -s http://127.0.0.1:8010/api/health     # {"ok":true,"loaded_states":N}
ss -ltnp | grep 8010                         # which pid holds the port

# watch the logs
journalctl --user -u lap-backend -f          # live, Ctrl-C leaves the log, the service keeps running
journalctl --user -u lap-backend -n 200      # last 200 lines
journalctl --user -u lap-backend --since "1 hour ago"
journalctl --user -u lap-backend -p err      # errors only
systemctl --user show lap-backend -p NRestarts -p ActiveEnterTimestamp   # how often it restarted, since when

# control it
systemctl --user restart lap-backend         # after changing backend code
systemctl --user stop lap-backend            # stop (it stays stopped until started again)
systemctl --user start lap-backend
systemctl --user disable --now lap-backend   # remove from autostart as well

# development with --reload: stop the service first so the port is free, then run by hand
systemctl --user stop lap-backend && LAP/web/run_backend.sh --reload
# ... and when finished: systemctl --user start lap-backend
```

If something died "every day", `journalctl --user -u lap-backend --since yesterday` shows why, and `NRestarts` shows
how many times systemd had to bring it back. Do not start a second copy by hand while the service is running: both
want port 8010 and the second one fails with `Address already in use` (`status.sh` shows who holds it).

Other terminal views:

```bash
tmux ls                                      # tmux/Byobu sessions (the dev server for the frontend may still live in one)
tmux attach -t lapview                       # Byobu: prefix is Ctrl-a (not Ctrl-b); detach with Ctrl-a d or F6
tmux capture-pane -p -t lapview -S -100      # print the last 100 lines of a pane without attaching
```

**Open to the whole network:** the service runs with `HOST=0.0.0.0` (drop-in
`~/.config/systemd/user/lap-backend.service.d/network.conf`), so anyone on the network can open
`http://172.16.60.60:8010` directly, with no tunnel. The dashboard has **no login**: everyone who can reach the
port can search, download and drop state tables. To go back to local-only, delete that `network.conf`, then
`systemctl --user daemon-reload && systemctl --user restart lap-backend`.

Otherwise, to look at the dashboard from your own PC, forward the port (the backend listens on the server only):
`ssh -N -L 18010:127.0.0.1:8010 cigma@172.16.60.60`, then open `http://localhost:18010`.

## How a search works

1. The file is read with GDAL (`ogrinfo`/`ogr2ogr`) and reprojected to EPSG:4326. KML with
   several folders, zipped shapefiles and loose `.shp/.dbf/.shx/.prj` parts all work.
   A shapefile with no `.prj` is assumed to be lon/lat and rejected if the numbers look projected.
2. All shapes in the upload are unioned inside PostGIS and matched against the GIST index
   on `cadastral.parcels.geometry`:
   * **Intersecting** - `ST_Intersects` (any overlap or touch; for a line, parcels it passes through)
   * **Fully inside** - parcel completely contained in the area
   * **Crossing the boundary** - intersects but is not fully inside
3. Rows stream from a server-side cursor into `data/results/<id>/result.geojson` (flat memory use).
4. The map loads that file and splits it by `source` (the original Parquet file): **each source
   file is its own map layer** with its own colour, visibility, opacity, stacking order
   (up/down arrows) and zoom-to. Click a parcel to see attributes from every visible layer under it.
   Every layer row (parcel source files, the area outline, the strip, the inside parts) has a
   colour picker; your colour for a source file is remembered for later searches in the same page.
5. Downloads are converted from the result with `ogr2ogr` on first request. Pick **All source
   layers together**, or a single source file in the selector above the format buttons. For
   GeoPackage there is also *one layer per source file* in a single file.

Every original attribute column from the source Parquet is kept (stored as `jsonb` in `attrs`
and flattened into properties). Three fields are added: `parcel_id`, `state`, `source`.
Shapefile format cuts column names to 10 characters; use GeoPackage/GeoJSON to keep full names.

## Two lines = a strip

If the uploaded file holds **exactly two lines** (two LineStrings, or one MultiLineString of two
parts) they are read as the two edges of a strip. The second line is flipped automatically if it
was drawn in the opposite direction, the lines are joined into one polygon, and that polygon is
what parcels are matched against (untick *Two lines = the strip between them* to search the lines
themselves instead). Lines that cross each other give a bow-tie of two triangles.

For every matched parcel the app measures, on the WGS84 ellipsoid, in square metres:

* `area_m2` - the whole parcel,
* `inside_m2` - the part of it inside the strip (or inside the polygon, for polygon searches),
* `inside_pct` - the share of the parcel that is inside.

The part inside is also kept as a clipped geometry (layer *Parcel parts inside the area*; download
*Only the part inside*). Totals are shown per source file. The same land appears once per source,
so the combined "All" row counts it more than once; use one source's row for a true area.

### Parcel table CSV

The parcel table is **one CSV per source file** (four for Goa), each with exactly three columns:
`total area`, `area affected`, `parcel number` (square metres; one row per parcel with some area
inside; parcels that only touch the edge are left out). With "All source layers" selected the button
gives a .zip holding a CSV for every source file loaded for the searched states (header only if a
source has nothing inside the area); choose one source in the selector to get just its CSV.
The parcel number comes from a different column per source file, set in
`backend/app/config.py` (`PARCEL_NUMBER_COLUMNS`):

| Source file | Column |
|---|---|
| `Goa_Bhunaksha_Cadastrals` | `Plot` |
| `NBSS_Goa_Cadastrals` | `Survey_No` |
| `Onemapgoa_GA_Cadastrals` | `plot` |
| `Onemapgoa_GA_Cadastrals_Mar2026` | `plot` |

Add a line there for every new source file. Until then a new source falls back to the first of
plot / survey_no / survey / khasra / parcel_num / kide that it has. Pick a single source in the
selector to get a table for that source only. Excel may turn values like `12/3` into dates when it
opens the CSV directly; import the column as Text to avoid that.

## Village boundaries

Every search also returns the village boundaries that match the same area (same match mode, same
state filter), as **one separate layer** drawn under the parcels. It has its own colour, opacity and
zoom-to, and clicking anywhere in a village shows its details (village, sub-district, district, LGD
codes and `area_m2` / `inside_m2` / `inside_pct`, the area of the village and the part inside the
search area). The result panel lists villages per state with their areas, and the download selector
has a *Village boundaries* entry (GeoJSON, GeoPackage, Shapefile, KML, CSV, optionally clipped).
A search is capped at 20,000 villages (`MAX_VILLAGES`); beyond that the village layer reports the
limit while the parcels are still returned.

The data is in the table `cadastral.villages` (same columns as the parcels: `state`, `source`,
`attrs` jsonb, `geometry` EPSG:4326). It is loaded from `data/mount/Village_boundaries/*.zip` by

```bash
cd LAP
lapenv/bin/python src/load_villages.py              # every zip not yet loaded; resumable
lapenv/bin/python src/load_villages.py --only GOA   # one file
```

The shapefiles are in projected systems (mostly a custom Lambert LCC, Gujarat in Mercator, Punjab
in UTM) and many are 3D; the loader reprojects to EPSG:4326, drops Z and makes everything
multipolygon. Attribute names differ between states (`Vill_Cat`/`Vill_cat`, `Sub_dist`/`Subdist`,
`Villl_name` in Madhya Pradesh), so they are stored under one set of names: `village`, `vill_lgd`,
`vill_category`, `sub_district`, `subdis_lgd`, `subdis_type`, `district`, `dist_lgd`, `state_ut`,
`state_lgd`, `remarks`. The state label of each zip is the name of that state's parcel folder, so
one state filter covers both layers (the zip-to-folder names are in `STATE_LABELS` in the loader;
if a parcel folder is renamed, update the label and `UPDATE cadastral.villages SET state = ...`).
A zip that gets updated can be reloaded by deleting its rows and its line in
`cadastral.village_load_log`, then rerunning the loader.

## Villages without parcels

Of the villages a search finds, those that have **no cadastral parcel in our database** are listed separately.
A village counts as having a parcel when the centre point (centroid) of at least one parcel of its state lies
inside the village boundary (a parcel only touching the village's edge from the next village does not count).
The check runs at the end of every search, while the state's parcel table is still in the database
(`query.villages_without_parcels`, called from `search._villages_without_parcels`).

* **On the map** they are a layer of their own, *Villages without parcels*, drawn over the green village
  boundaries in orange (colour, opacity, zoom-to like the other layers). Click one for its details; the
  `no_parcels_reason` field says why.
* **Download:** the button *Villages without parcels (CSV, n)* in the Download section gives one CSV, sorted by
  state, district, taluka and village, with the columns state, district, taluka, village, village / taluka /
  district LGD codes, village category, village area (m2), area inside the searched area (m2 and %), and
  reason. API: `GET /api/results/<id>/download?layer=villages_no_parcels&format=csv`; the polygons are at
  `GET /api/results/<id>/features?layer=villages_no_parcels`.
* **Reasons:** *no parcel data for this state* (the state has no parcel files, e.g. Uttarakhand: every village found
  there is listed), *parcel data for this state is not loaded* (it has parcel files but this search did not load
  it, only for a village just outside the detected states), or *no parcel lies inside this village*.
* A failure of this check never fails the search; the result then carries `villages.no_parcels.error`.

## DXF alignment and chainage

Instead of an area file, the dashboard takes a **DXF alignment** and, optionally, a second **DXF with chainage
labels** (choose *DXF alignment + chainage* under *1. Area of interest*). Code: `backend/app/dxf.py` (reading),
`backend/app/chainage.py` (chainage per parcel), `frontend/src/DxfInputs.tsx` (the form).

**Coordinate system.** A DXF does not store one, so the form always asks. The list puts first the CRS defined in
the file (if GDAL finds one) and then the ones that put the drawing on land in a state (the check uses
`cadastral.state_boundaries`; e.g. UTM zone 44N for a drawing near Hyderabad), then the rest (WGS 84, UTM 42N-46N,
Kalianpur, India NSF LCC, Web Mercator) and *Other EPSG code*. The chosen CRS is applied with GDAL when the DXF is read.
The alignment and the chainage file each have their own CRS box (the chainage one starts as the alignment's).

**Alignment.** Of all lines and polylines in the file only the longest are used; every shorter line (tick marks,
labels' outlines, centreline pieces) is a marking and is ignored. *Detect* takes two lines when the second longest
is at least half as long as the longest, else one; *Two lines* (the corridor's edges, searched as the strip between
them, as for any two-line file) and *A single centreline* force it. The form says how many lines were used and
ignored, and warns when the next longest line is nearly as long. Arcs are not straightened: GDAL cuts arcs and
polyline bulges into segments of 0.05 degrees (`OGR_ARC_STEPSIZE`; its default of 4 degrees shortened 77 km of curves
by 9 m), so the strip area is that of the real curves.

**Chainage.** Text points (TEXT / MTEXT) whose value in the chosen column starts with **CH** or **KM**. The form shows
every text column with how many values are filled and how many start with CH/KM, the first 10 text rows of the
file, and flags an empty column; the column with the most CH/KM values is preselected. Readable forms: `CH -2+400`,
`KM: 12+500`, `CH 12+345.5` (kilometres + metres, a minus sign makes the whole value negative), `KM 12.5` (kilometres),
`CH 12500` (metres), `CH 5 km`. A label that starts with CH/KM but holds no readable chainage is counted and reported.
Other texts (for example `TPTC1 AT CH ...`) are left out.

**What the parcel table gets.** Three more columns, after *district*:

| Column | Meaning |
|---|---|
| `chainage from` / `chainage to` | where the part of the parcel inside the strip (for a centreline or a plain lines search: the part the line crosses) begins and ends along the alignment, written like `CH 23+581` (a `CH` prefix also keeps a spreadsheet from reading it as a formula) |
| `chainage labels inside` | the labels (as written in the DXF) that lie inside the parcel, in chainage order, separated by `;` |

The distance along the alignment is measured on a centreline in UTM (for two lines the average of the two edges),
the labels within 500 m of it are projected onto it, and the chainage of any position is interpolated linearly
between the two labels around it (beyond the first or last label it continues at one chainage metre per metre).
Labels with the same chainage count once; if the labels do not run in order along the alignment the chainage is not
applied and the result says why (the search itself still succeeds). A search along a **single centreline** has no area:
the parcel table then lists every parcel the line crosses with *area affected* empty.

On the map the labels are a layer of their own (blue dots, with the text from zoom 12; the font comes from
demotiles.maplibre.org, without it only the dots show). The uploaded DXFs are kept on the server for 6 hours
(`data/dxf_uploads`), so choosing a CRS, previewing and searching do not upload a large file again. API:
`POST /api/dxf/upload`, `POST /api/dxf/preview`, `POST /api/query` with `alignment_token`, `crs`, `align_mode`
(`auto|two|single`), and optionally `chainage_token`, `chainage_crs`, `chainage_field`;
`GET /api/results/<id>/features?layer=chainage`. DWG files are not supported (save them as DXF).

## Type correction

About 85% of the attribute columns in the parcel parquet files are declared as strings, including
columns that really hold numbers (`shape_area`, `area_ha`, `taluka` codes ...), so they reached the
map and the downloads as text. `backend/app/typecast.py` corrects this **before any result is
produced**: as each parcel's attributes are read from the database (`query._properties`) the numeric
columns are converted to numbers. Nothing is reloaded, and values that are already numbers pass through.

Which columns are numeric comes from `backend/column_types.json`, written by `src/audit_types.py`
after checking the values of every column in every parquet file (the full per-column report is
`data_type_audit.csv` in the project folder: declared type, what the values really are, % numeric,
an example). The rules are deliberately cautious:

* only columns whose values are **all** numeric are converted (196 whole-number and 80 decimal
  columns across 55 files, 276 in total);
* a value is converted only when that loses nothing: whole numbers without leading zeros and up to
  15 digits, or ordinary decimals. Codes like `05614`, parcel numbers like `12/3` and long digit
  strings stay text; columns that are 99.9% numeric stay text too (see below);
* applying it twice does nothing extra.

`src/load_cadastral.py` uses the same module, so states loaded from now on are stored with the
numbers already typed. To refresh the map after new files are added: `python src/audit_types.py`.

Per-source downloads (single source, or GeoPackage layer per source) are built from that source's
own rows only, so each file has exactly its own columns with their own types; "all sources together"
is the union of every source's columns (a column that is a number in one source and text in another
becomes text there).

**Source-data defect found by the audit:** in `Goa_Bhunaksha_Cadastrals`, `Shape_Area` and
`Shape_Leng` are 99.9% numeric, but about 960 rows hold a parcel number (`45/1`, `101/67-A`) in
those columns. Because of those rows the column is left as text rather than converting the rest.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` | `172.16.60.60` / `5432` / `Vikash` / `geouser` | database connection |
| `PGPASSWORD` | (unset) | optional; otherwise `~/.pgpass` is used |
| `MAX_FEATURES` | `100000` | most parcels one search may return |
| `MAX_UPLOAD_MB` | `200` | upload size limit |

The database session is read-only with a 5-minute statement timeout.

## Parcel data is loaded on demand (and dropped again)

The parcel files add up to about 200 GB in the database, which does not fit, so they are **not**
kept there. A search loads only the state(s) the uploaded area falls in:

1. The upload is read and the state(s) it touches are found from `India_State_Boundary.zip`
   (loaded once into `cadastral.state_boundaries`; a state whose boundary is within 1 km of the area
   counts, for coasts and borders). An area outside every state, e.g. at sea, is reported as such.
2. For each of those states whose parcels are not in the database yet, all of its parquet files are
   loaded into their own table `cadastral.parcels_<state>` (type correction applied, spatial index
   built). The search runs as a background job and the dashboard shows the progress, with an estimate.
   A state with no parcel files (Uttarakhand, Sikkim, Himachal...) is skipped: you still get its villages.
   Two searches that need the same state share one load.
3. Parcels and villages are queried (several state tables at once if the area spans states).
4. **3 hours after a state was last used** its whole table is dropped by a sweeper thread inside the
   backend (checked every minute, and once at start-up; `STATE_TTL_SECONDS`). Using a state in a search
   restarts its clock. A state a search is using is never dropped under it.
5. The dashboard's *Parcel data in the database* panel lists what is loaded, with size and time until
   it is dropped, a **Drop** button per state and **Clear all**. Both drop the whole state table (after a
   confirmation); they refuse while a search is using or loading that state.

Safeguards: before loading a state its size is estimated (3.8 x the parquet size, measured on Goa) and
the load is refused if the loaded tables plus the new one would exceed `STORAGE_BUDGET_GB` (200 by default;
SQL cannot see the server's free disk, so this budget is the guard). Estimated sizes in the database:
Goa 2.4 GB, Delhi 0.2, Bihar 12, Rajasthan 15.5, Jharkhand 21, Odissa 27, Madhya Pradesh 65 GB; the panel
lists every state with its estimate. Loading runs at roughly 15-20 thousand parcels a second, so a large
state takes a long time (Goa, 2.6 million parcels, about 2.5 minutes; Odissa, 47 million, about 45).

Already-made results keep working after their tables are dropped: the result files and the interior
points the village lookup needs are stored with the result (kept 24 hours).

Village boundaries and the state boundaries are permanent (about 1.8 GB together).

Command line, same code: `lapenv/bin/python src/load_cadastral.py --list | --state Goa | --drop Goa | --sweep`.
The sweeper only runs while the backend does. To also drop expired tables when the backend is stopped, run
`python src/load_cadastral.py --sweep` from cron, e.g. `*/10 * * * * cd LAP &&
lapenv/bin/python src/load_cadastral.py --sweep >> sweep.log 2>&1` (not installed; add it with `crontab -e`).

`cadastral.load_log` (the single-table loader's log) is no longer used and can be dropped.
