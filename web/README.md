# Cadastral Parcel Finder

Upload an area of interest (KML, KMZ, Shapefile, GeoJSON, GeoPackage) and get every
cadastral parcel that intersects it, shown on a basemap and downloadable as GeoJSON, GeoPackage,
Shapefile, KML or CSV. All the parcel data lives permanently in PostgreSQL/PostGIS and is searched there directly;
one script, `src/sync_parcels.py`, copies the parquet files into it and brings it up to date (see *Parcel data is
in the database*).

Same stack as `land_map_merge/web`: FastAPI backend + Vite/React/TypeScript/MapLibre frontend.

```
web/
  backend/app/   main.py (API)  search.py (the search job)  query.py  exports.py  config.py  aoi.py  linear.py  alignment.py
                 dxf.py  chainage.py  loader.py (the sync engine)  statedata.py (what is in the database)  footprint.py  priority.py  villageparcels.py
                 enrich.py  pieces.py  typecast.py  auth.py (sign-in)
  frontend/src/  App.tsx  MapView.tsx  api.ts  basemaps.ts  Header.tsx  InputFile.tsx  PriorityDialog.tsx  ProgressBar.tsx
                 Resizer.tsx  index.css (all styling, light and dark)
  backend/login.html   the static sign-in page
  data/results/  per-search result files (auto-deleted after 24 h)
```

## Conventions used below

Nothing in the project depends on where it is installed, and no address, port, user or database name is built in. In the commands
below these placeholders stand for your own values (set them once in your shell, or substitute them):

| Placeholder | Meaning |
|---|---|
| `$LAP` | the project folder (the one that holds `compose.yaml` and `src/`): `export LAP="$(pwd)"` when you are in it |
| `$PGHOST` `$PGPORT` `$PGDATABASE` `$PGUSER` | the PostgreSQL server, port, database and user (the standard libpq variables) |
| `$DB_PASSWORD` | the database user's password (only ever typed into the password file, never stored in the project) |
| `$MOUNT_ROOT` | the data folder: one sub-folder of parquet files per state, plus `Village_boundaries/` and `India_State_Boundary.zip` (setting `MOUNT_ROOT`) |
| `$SHARE` | the network share folder that holds that data, as `//FILE_SERVER/share/path` |
| `$HOST`, `$PORT` | where the backend listens (settings `HOST`, `PORT`) |
| `$SERVER` | this machine's name or address, as seen from your PC |
| `$SSH_USER`, `$LOCAL_PORT` | your login on the server, and a free port on your own PC (for the tunnel) |

The defaults of the settings that have one are listed in *Settings (environment variables)*.

## Setting up on a new machine

The steps are for Ubuntu/Debian. The order is: Python environment, PostgreSQL, the data, load it into the database, start the app.

### 1. The Python environment

`$LAP/setup.sh` creates the environment `lapenv` in the project folder from `requirements-lock.txt` (the exact versions the project
was built with; needs Python 3.13 with venv, and GDAL's `ogr2ogr`/`ogrinfo`: `sudo apt install gdal-bin`).

### 2. PostgreSQL with PostGIS on this machine

Skip the install if you use a database on another server; then only do the connection settings below (and ask its
administrator for a database, a user that owns it, and the PostGIS extension).

```bash
export PGHOST=<database server; "localhost" if it runs on this machine> PGPORT=<port, normally the PostgreSQL default>
export PGDATABASE=<database name> PGUSER=<user name>      # your choice; put them in your shell profile
read -rs -p "Choose the database password: " DB_PASSWORD; echo                       # typed, not stored anywhere

sudo apt install postgresql postgis                       # the server and PostGIS
sudo -u postgres psql -c "CREATE ROLE $PGUSER LOGIN PASSWORD '$DB_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE $PGDATABASE OWNER $PGUSER;"
sudo -u postgres psql -d "$PGDATABASE" -c "CREATE EXTENSION IF NOT EXISTS postgis;"   # needs the postgres superuser
```

The password comes from the libpq password file (`$PGPASSFILE`, by default `.pgpass` in your home folder), mode 600, never from the project:

```bash
PGPASS="${PGPASSFILE:-$HOME/.pgpass}"
echo "$PGHOST:$PGPORT:$PGDATABASE:$PGUSER:$DB_PASSWORD" >> "$PGPASS" && chmod 600 "$PGPASS"
psql -c "SELECT postgis_version();"                       # a version number means it works
```

The user must own the database (the scripts create the `cadastral` schema and the tables in it). The app reads only that schema.

Disk: the database needs about 4 times the size of the parquet files (a state with 17 GB of parquet takes about 65 GB), plus room
while a state is being indexed. If PostgreSQL's data directory is on a small disk, move it to the big disk first (`data_directory`
in `postgresql.conf`; stop the server, `rsync -a` the old directory over, start it).

For **Docker** the app container must reach the database, so "this machine" does not work there: set `PGHOST` in the `.env` file to the
server's own address, make PostgreSQL listen on it (`listen_addresses = '*'` in `postgresql.conf`), allow the Docker network in
`pg_hba.conf` (a line `host <database> <user> <docker subnet> scram-sha-256`; the subnet is in the output of
`docker network inspect <project name>_default`), and restart PostgreSQL. Scripts run on the host (`sync_parcels.py`, the loaders)
keep their own `PG*` variables and can address the server as this machine. Copy `.env.example` to `.env` next to `compose.yaml` and fill it in.

Optional tuning for big loads (`postgresql.conf`): `shared_buffers` about a quarter of the RAM, `maintenance_work_mem` of 2GB or more
(index builds), `max_wal_size` of 8GB or more.

### 3. The data: a local folder, or the network location

**Option A, the data is on this machine's disk:** copy it into the project's `data/mount` folder, or leave it where it is and
`export MOUNT_ROOT=<the data folder>`.

**Option B, the data is on a network share (Windows/Samba file server):** mount it read-only at `$MOUNT_ROOT`
(by default the project's `data/mount`).

```bash
sudo apt install cifs-utils
CREDS="$HOME/.smbcreds"                                   # any file name; mode 600
printf 'username=<share user>\npassword=<share password>\n' > "$CREDS" && chmod 600 "$CREDS"   # add domain=... if needed

# permanent (survives reboots; `nofail` so a missing file server never blocks booting).
# $SHARE is the share folder in the form //FILE_SERVER/share/path (the Windows path \\FILE_SERVER\share\path):
sudo "$LAP/mount_share.sh" "$SHARE" "$CREDS" "${MOUNT_ROOT:-$LAP/data/mount}"

# or just once, until the next reboot:
sudo mount -t cifs "$SHARE" "${MOUNT_ROOT:-$LAP/data/mount}" \
     -o credentials="$CREDS",ro,vers=3.0,uid=$(id -u),gid=$(id -g)
```

`mount_share.sh` (the last two arguments are optional) adds the `/etc/fstab` entry (after backing the file up), mounts it, and
checks that what is mounted is the right folder and holds `India_State_Boundary.zip` and `Village_boundaries/`. Check by hand with
`ls "${MOUNT_ROOT:-$LAP/data/mount}"`: you should see the state folders. `sudo umount` of the same folder unmounts it. The share is
only needed while the sync script runs: the app reads the database, never the share.

### 4. Load the data into the database

```bash
cd "$LAP"
lapenv/bin/python src/load_state_boundaries.py        # which state an area falls in (once)
lapenv/bin/python src/load_villages.py                # village boundaries (once; resumable)
lapenv/bin/python src/sync_parcels.py --dry-run       # what would be loaded
lapenv/bin/python src/sync_parcels.py --jobs 4        # the parcels (see "Parcel data is in the database")
```

Run `sync_parcels.py` again whenever data is added to the folder or share; it only loads what is new or changed.

### 5. Start the app

`"$LAP/web/run_backend.sh"` serves the API and the built frontend on `http://$HOST:$PORT`; after changing frontend code run
`"$LAP/web/build_frontend.sh"`. For the HTTPS version with sign-in see *Running in Docker*.

## Run it

```bash
$LAP/web/run_backend.sh        # API and the built frontend, one process, http://$HOST:$PORT
$LAP/web/status.sh             # is it running? (tells this project's backend apart from other uvicorns)
```

The backend listens on the server itself only. To open it from your own PC, tunnel the port, e.g.
`ssh -N -L $LOCAL_PORT:localhost:$PORT $SSH_USER@$SERVER` and browse to `http://localhost:$LOCAL_PORT` (or forward the port in
VS Code). To make it reachable from other machines start it with `HOST=0.0.0.0` (it still asks for a sign-in, see
*Running in Docker*; `LAP_AUTH=off` removes that, for local development only).

Only while changing the frontend: `$LAP/web/run_frontend.sh` starts the dev server (on its own port, printed when it starts; proxying /api to the
backend), and `$LAP/web/build_frontend.sh` rebuilds the version the backend serves.

**Remember when developing:** the backend serves the *built* frontend (`frontend/dist`), so every change under
`frontend/src` needs `build_frontend.sh` and then a hard refresh (Ctrl+F5); a change to Python code needs the backend
restarted. A second copy for development can run next to the Docker one on another port, e.g.
`HOST=0.0.0.0 PORT=<another port> ./run_backend.sh`. The Docker container (`https://$SERVER:<HTTPS port>`) only changes after
`docker compose up -d --build`.

First time only: `$LAP/setup.sh`, see *Setting up on a new machine*.

## Running in Docker (how it runs now)

The dashboard runs as two containers from `$LAP/compose.yaml` (project `lap`), which replaced the systemd service below:

| Container | What it is |
|---|---|
| `lap-gateway` | nginx: **HTTPS** (self-signed certificate). The only part reachable from the network. |
| `lap` | the app (API + built dashboard + its own **sign-in page**), one process. Not published: only the gateway can reach it. |

**Address:** `https://$SERVER:<HTTPS port>` (`LAP_HTTPS_PORT`). Plain `http://$SERVER:<HTTP port>` (`LAP_HTTP_PORT`) redirects there. The browser warns once about the
self-signed certificate; to avoid it, import `deploy/certs/lap.crt` as a trusted certificate on the PC (or replace the two
files in `deploy/certs/` with a certificate from your own CA, then `docker compose restart gateway`).

**Signing in.** Everyone sees a sign-in page (`/login`, a small static page: `backend/login.html`) and needs a user name and
password. A successful sign-in sets a signed session cookie (HttpOnly, Secure, SameSite=Lax) that lasts 12 hours
(`SESSION_HOURS`); the dashboard shows who is signed in and has a **Sign out** link, and a session that ran out sends the
person back to the sign-in page. Without a valid session every page redirects to `/login` and every `/api` call answers
401; only the container's own loopback health check is let through. After 8 wrong passwords an address is blocked for 5
minutes (nginx also limits sign-in attempts to 20 a minute). The code is `backend/app/auth.py`.

The users and their password hashes (scrypt) are in `deploy/auth/users`; manage them on the server (changes take effect
at once, no restart, and a user that is removed is signed out immediately):

```bash
cd "$LAP"
deploy/users.sh list
deploy/users.sh add <name>        # asks for the password twice; run it again to change a password
deploy/users.sh remove <name>
```

The first user, `admin`, was created with a random password stored in `deploy/initial_admin_password.txt`. Set your own with
`deploy/users.sh add admin`, then delete that file.

**Everyday commands** (run in `$LAP`):

```bash
docker compose ps                         # both containers, with the health of the app
docker compose logs -f lap                # live log of the app (Ctrl-C leaves the log, it keeps running)
docker compose logs --tail 100 gateway    # sign-in attempts and requests
docker compose restart lap                # restart the app
docker compose up -d --build              # after a code change: rebuild the image and replace the container
docker compose down                       # stop everything (docker compose up -d starts it again)
docker exec -it lap bash                  # a shell inside the app container
"$LAP/lapenv/bin/python" "$LAP/src/sync_parcels.py" --list           # what the database holds (run the sync script on the host)
```

Both containers restart by themselves after a crash or a reboot (`restart: unless-stopped`; Docker starts at boot).
For a foreground run with the log in your terminal use `docker compose up` (without `-d`).

**What is mounted into the app container**

| Host | Container | Why |
|---|---|---|
| `$LAP/web/data` | `/app/web/data` | search results (kept 24 h) and uploaded DXFs (kept 6 h) |
| `$LAP/deploy/auth` (read-only) | `/app/auth` | the users who may sign in |
| the libpq password file (read-only) | the same file for the container user | the database password |

The container runs as the user and group ids `LAP_UID` / `LAP_GID` (set them in `.env` to the output of `id -u` / `id -g` of the user that
owns the project) so these folders keep the right ownership. The database is given by `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` in
the `.env` file next to `compose.yaml` (see `.env.example`; `MAX_UPLOAD_MB` and the ports `LAP_HTTPS_PORT` / `LAP_HTTP_PORT` can be set there too). To cap the memory of a very large search,
uncomment `mem_limit` in `compose.yaml`.

**Files:** `Dockerfile` (stage 1 builds the dashboard, stage 2 is Python 3.13 + GDAL 3.10 + the backend), `compose.yaml`,
`.dockerignore`, `deploy/nginx.conf.template`, `deploy/make_cert.sh` (makes the certificate), `deploy/users.sh`. The certificate,
the users file and `.env` are in `.gitignore`. If the users file is missing or empty nobody can sign in (the dashboard stays
locked, it is never opened by mistake). Setting it up on another server: `deploy/make_cert.sh`, `deploy/users.sh add <name>`,
make sure the libpq password file exists and the database holds the parcel data (`src/sync_parcels.py`), then `docker compose up -d --build`.

**Going back to the systemd service** (below), if ever needed: `docker compose down`, then
`systemctl --user enable --now lap-backend`. It also asks for a sign-in (same users file), but over plain http on `$PORT`, so
use it only temporarily.

**Developing without signing in.** Sign-in applies to every way of starting the backend. For local development (for
example `run_backend.sh` or the Vite dev server, whose pages are not served by the backend) start the backend with
`LAP_AUTH=off ./run_backend.sh`; never do that on a server other people can reach.

## Running it as a service (systemd) and watching it in the terminal

> **Note:** the systemd service described in this section is the previous way of running the dashboard. It is disabled now and
> the dashboard runs in Docker (previous section). The commands below remain valid if you switch back.

The backend runs as a systemd **user** service, `lap-backend`, so it no longer lives in a tmux pane: it restarts by
itself 5 seconds after any crash (`Restart=always`), starts again after a reboot, and survives SSH logouts
(`loginctl enable-linger $USER` is on). The unit file is `lap-backend.service` in your systemd user unit folder; it runs
`web/run_backend.sh` (so `HOST` / `PORT` still apply, set them with `systemctl --user edit lap-backend`:
`[Service]` then `Environment=PORT=$PORT`).

```bash
# is it running? (no sudo needed for any of these)
systemctl --user status lap-backend          # running or failed, pid, memory, last log lines
$LAP/web/status.sh                            # same question plus a /api/health check, and other uvicorns on the box
curl -s http://$HOST:$PORT/api/health     # {"ok":true,"loaded_states":N}
ss -ltnp | grep $PORT                         # which pid holds the port

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
systemctl --user stop lap-backend && $LAP/web/run_backend.sh --reload
# ... and when finished: systemctl --user start lap-backend
```

If something died "every day", `journalctl --user -u lap-backend --since yesterday` shows why, and `NRestarts` shows
how many times systemd had to bring it back. Do not start a second copy by hand while the service is running: both
want the same port and the second one fails with `Address already in use` (`status.sh` shows who holds it).

Other terminal views:

```bash
tmux ls                                      # tmux/Byobu sessions (the dev server for the frontend may still live in one)
tmux attach -t lapview                       # Byobu: prefix is Ctrl-a (not Ctrl-b); detach with Ctrl-a d or F6
tmux capture-pane -p -t lapview -S -100      # print the last 100 lines of a pane without attaching
```

**Open to the whole network:** the service runs with `HOST=0.0.0.0` (drop-in
`network.conf` in the `lap-backend.service.d` folder next to it), so anyone on the network can open
`http://$SERVER:$PORT` directly, with no tunnel, over plain http (the sign-in page applies, but the password
travels unencrypted). To go back to local-only, delete that `network.conf`, then
`systemctl --user daemon-reload && systemctl --user restart lap-backend`.

Otherwise, to look at the dashboard from your own PC, forward the port (the backend listens on the server only):
`ssh -N -L $LOCAL_PORT:localhost:$PORT $SSH_USER@$SERVER`, then open `http://localhost:$LOCAL_PORT`.

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
`attrs` jsonb, `geometry` EPSG:4326). It is loaded from `$MOUNT_ROOT/Village_boundaries/*.zip` by

```bash
cd "$LAP"
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

## Layer priority

A state's parcels can come from several source files (layers) that map the same land. In the *Match* step the **Layers** window
(`PriorityDialog.tsx`) lets you check which layers to search (unchecked layers are left out of the search completely; the
window draws where each layer has parcels and checks the ones that cover your area) and rank them (the checked layers in
order are sent as `inputs.priority`; a layer with no parcels near the area simply gives way to the next one, because
nothing of it is there to cover the lower layers). The top layer keeps all its parcels; from each lower
layer only the parcels the higher ones do not already cover are kept: a parcel is dropped when **more than 50%** of its area
lies inside the kept parcels of higher layers (`priority.DROP_FRACTION`). It is computed in PostGIS against the state table
(`backend/app/priority.py`), runs only when two or more layers are ranked, and the result's `meta.json` records the order and
how many parcels each layer lost (`priority.dropped`). The map and downloads then show one parcel per place; the `priority`
rank of a parcel's layer is a column.

## Whole village parcels (export)

*Export, Parcel polygons* has three kinds: **Whole parcels** (every parcel the search found, complete), **Only the part inside**
(clipped to the searched area, only for area/strip searches) and **Whole village parcels**: every parcel in the villages the
alignment, area or right of way passes through, not only the ones that meet it (shown when the search found villages).

* A parcel belongs to a village when its **centroid** lies inside the village boundary (the same rule as *Villages without
  parcels*), and the layer priority of the search is applied.
* The LAP columns (village, taluka, district, state, parcel number) are filled; `area_m2` is the whole parcel, `inside_m2` /
  `inside_pct` the part inside the searched area (0 for parcels outside it). There are no chainage columns for parcels outside
  the strip.
* It uses the file formats and source-layer selector of the other polygon exports. Files are named
  `cadastral_village_parcels_...`. API: `GET /api/results/<id>/download?layer=village_parcels&format=...` (not `table`).
* Code: `backend/app/villageparcels.py`. It builds `data/results/<id>/village_parcels/` (`result.geojson`, `meta.json`) once,
  reloading a state's parcel table if it was dropped meanwhile, and the normal export code converts that. **The first request
  is slow** (about 2 minutes for 75 villages / 33,000 parcels) and later ones are instant; it goes away with the result after 24 h.
* The size cap is `MAX_FEATURES` (100,000 parcels): a long alignment through many villages can exceed it and gets the standard
  "too many parcels" answer (413); a shapefile/KML of that many parcels is also very large (86 MB KML for 33,000).

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
* **Reasons:** *no parcel data for this state* (the database holds no parcels for the state, e.g. Uttarakhand: every
  village found there is listed), or *no parcel lies inside this village*.
* A failure of this check never fails the search; the result then carries `villages.no_parcels.error`.

## Look and feel

The dashboard has a blue theme, all in `frontend/src/index.css` (colours are variables at the top): a header bar across the top
(logo, title with its "i" card, who is signed in and **Sign out**; markup in `App.tsx`), and every section of the side panel is a
**card with a title bar in its own shade of blue**: *1. Area of interest* blue (`.c-area`),
*2. Match* indigo (`.c-match`), *Result* cyan (`.c-result`), *Layers* navy (`.c-layers`). Change a section's colours by editing its
`--accent`, `--accent2` (title bar gradient) and `--tint` (card background). Buttons are raised and lift when hovered; the
classes are `primary` (the main actions: the CSV tables), `big`, `mini` (small actions in lists), `plain` (an icon
or arrow without a frame) and `on-dark` (on the header); a plain `<button>` gets the soft style. **Find parcels** is orange
(`primary big find`). The sign-in page (`backend/login.html`) carries the same colours in its own small stylesheet. After a
change: `docker compose up -d --build`.

**Glass side panel.** The side panel (`.sidebar`) floats over the full-width map: it is absolutely positioned and has
`background: rgba(0,0,0,0.10); backdrop-filter: blur(80px)`, so the map shows through, frosted. Only the panel is glass; the
cards on it are **opaque** (`--tint`), because text on a see-through card was unreadable over light map areas. To see more of
the map lower the blur (e.g. `blur(16px)`) or the alpha in `.sidebar`. The resizer sits in `.sidebar-resizer`; the map is not
shifted aside, so "zoom to result" can place things under the panel.

**Dark theme.** The **Dark / Light** button in the header (and top right of the sign-in page) switches the whole page. The
choice is saved in the browser (`localStorage` key `theme`) and applied before the first paint by a small script in
`frontend/index.html` and `login.html`, so there is no flash. It sets `data-theme="dark"` on `<html>`; the dark colours are the
`:root[data-theme="dark"]` block at the end of `index.css`, which overrides the colour variables (`--surface`, `--ink`, `--t1`..`--t3`,
`--rule`...) and the glass, cards, inputs, pop-ups and map controls. Use those variables, not fixed greys, in new styles. The
basemap itself is not darkened.

## Lines in an area file (the alignment)

A KML, KMZ, shapefile, GeoJSON, GeoPackage or GML file that holds **lines** is treated as an alignment, found automatically
(`backend/app/alignment.py`; the search and the map preview use the same step, so the map shows the lines that will be searched).
It is built for the messy files these usually are: the two edges of a corridor drawn as hundreds of short pieces, with gaps and
overlaps, in any order and direction, with elevation values, tick marks, side roads, stray points and other lines elsewhere,
over hundreds of kilometres, with the edges 5 to 150+ m apart.

1. **Flatten.** Every line is split into simple pieces (MultiLineStrings and geometry collections are taken apart), **elevation
   and M are thrown away completely** and everything is projected to horizontal metres (UTM). Repeated points and pieces shorter
   than 1 m are dropped and the pieces are simplified by 0.25 m. Points are ignored. From here only horizontal distances and
   directions are used, so lines crossing at different heights, or points with different elevation at the same place, are never
   merged: only the **ends** of pieces are ever joined.
2. **Join pieces.** Two ends join when they touch (1 m), or when they point at each other (opposite directions within 30 degrees),
   the gap is up to 40 m (or an overlap up to 40 m, which is trimmed) and the sideways offset is small (1.5 m + 5% of the gap, so a
   join can never jump to the other edge, which is at least 5 m away). Each end joins one other end, best fit first.
3. **Find the corridor.** Lines are looked at every 40 m for a partner on each side: another line that runs in parallel (within 30
   degrees), roughly across, 4 to 400 m away. Chains with partners form the corridor; the group with the most side-by-side length
   wins, and groups whose ends line up across a break of up to 2 km are one corridor (an edge keeps its partner on the same side
   before and after the break, so the two edges are never crossed over). The chains are pointed the same way and **stacked across
   the corridor**, built from the strongest relations first so that a few metres of pinched corridor cannot overrule hundreds of
   kilometres. The two stacks with the most length are the edges; when they are about as long as a stack between them the
   outermost pair wins, so a **centreline between the edges is left out**, and so is a short service road or ramp beside one edge.
   Ticks, side roads and lines that do not run alongside another line are markings and are ignored.
4. **Bridge and choose.** Within each edge the chains are ordered along the route and joined across larger gaps: up to 5 km where
   the other edge shows the corridor goes on, up to 2 km where both edges are missing. A longer break in both edges starts a
   separate **section**; the longest section is used and the others are reported (a warning, and their lengths) so they can be
   searched separately, for example state by state. Both lines are then cut to the stretch where they run side by side.

If no two lines run side by side the older rule applies: the longest joined line, or the two longest when the second is at least
half as long as the longest. Two lines are searched as the strip between them unless *Two lines = the strip* is unticked. The
summary (the two lines' lengths, how far apart they run, how many pieces were joined, gaps bridged, lines ignored) is in the info
card of *1. Area of interest* and in the result.

**When it applies.** A file with any **polygon** is an area and is searched as drawn; a file with only points too; a file with
lines and points uses the lines. A **DXF** has its own rule (`dxf.py`): there the longest lines are taken without joining, because
a DXF drawing often stores the centreline in pieces next to the two edges.

**Tested** by `backend/tests/test_corridor.py` (run from `web/`: `../lapenv/bin/python backend/tests/test_corridor.py`; synthetic
corridors checked against the known true edges; no database needed): a 600 km winding route with edges
6 to 150 m apart, pieces of 1 to 6 km with gaps of up to 12 m and overlaps of up to 3 m, random order and direction, 60 ticks, 12 side
roads, a centreline in pieces, a decoy corridor and two long unrelated lines; missing stretches of one edge (bridged); a 1.2 km break
in both edges (bridged); a 5 km break (two sections); one edge missing its first 100 km; a hairpin with the two legs 300 m apart; a
drawing with a vertex every 5 m (548,000 vertices, 1.3 s). Each gives one line on each true edge, with the right length.

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

If one DXF holds both the alignment and the chainage labels, tick **Chainage is in the same DXF**: the chainage then uses the
alignment file (no second file or upload) and its coordinate system, and the text column is chosen the same way.

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

`src/sync_parcels.py` uses the same module, so data loaded from now on is stored with the
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
| `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` | this machine / PostgreSQL's default port / (libpq default) / (libpq default) | the database |
| `MOUNT_ROOT` | the `data/mount` folder inside the project | the data folder (read only by the sync script and the loaders) |
| `HOST` / `PORT` | `127.0.0.1` / `8010` | where `run_backend.sh` listens |
| `LAP_HTTPS_PORT` / `LAP_HTTP_PORT` | `8443` / `8010` | the ports of the Docker gateway (`.env`) |
| `PGPASSWORD` | (unset) | optional; otherwise the libpq password file (`PGPASSFILE`) is used |
| `MAX_FEATURES` | `100000` | most parcels one search may return |
| `MAX_UPLOAD_MB` | `200` | upload size limit |

The database session is read-only with a 5-minute statement timeout.

## Parcel data is in the database (`src/sync_parcels.py`)

All parcel data is kept **permanently** in PostgreSQL, one table per state (`cadastral.parcels_<state>`: `id`, `state`,
`source`, `attrs` jsonb, `geometry`, with a spatial index), and the app searches those tables directly. Nothing is loaded
or dropped while people use the dashboard, and there is no "Parcel data in the database" panel any more.

One script fills and updates the database, on the machine that has the data and the database:

```bash
cd "$LAP"
lapenv/bin/python src/sync_parcels.py                 # load whatever is new or changed in the data folder
lapenv/bin/python src/sync_parcels.py --dry-run       # only show what it would load
lapenv/bin/python src/sync_parcels.py --jobs 8        # load 8 files at the same time (default 2)
lapenv/bin/python src/sync_parcels.py --state "<state folder>"   # one state folder (repeat for more); --only TEXT = files containing TEXT
lapenv/bin/python src/sync_parcels.py --prune         # also remove files that are no longer in the folder
lapenv/bin/python src/sync_parcels.py --force         # reload files even if they look unchanged
lapenv/bin/python src/sync_parcels.py --list          # what the database holds
```

Run it for the first load and again **whenever parquet files are added or changed**; each run only does what is new:

* every loaded file is recorded in the catalog `cadastral.parcel_files` (state, file, size, modification time, rows and
  its footprint). A file with the same size and time is skipped; a new file is loaded; a changed file has its old rows
  replaced;
* a file is loaded in one transaction, so stopping the script (Ctrl-C, a crash, a reboot) never leaves half a file: run
  it again and it carries on with what is missing;
* a state's folder name gives its table; new state folders are picked up automatically, and the spatial index of a
  table is built once, after its new files are in. Files are loaded in parallel (`--jobs`; loading is mostly CPU, so use
  about as many as the server has cores to spare);
* for every file it also stores where the file has parcels (the *footprint*, about 2 km grid). The dashboard's **Layers**
  window draws it as a map and checks the layers that cover the area, so only those are searched;
* the type correction (*Type correction*) is applied while loading, as before.

Measured: Goa (4 files, 2.6 million parcels) loads in about 2 minutes with 4 workers. The database needs roughly 4 times
the size of the parquet files (a state of 17 GB parquet is about 65 GB).

Data folder layout: `MOUNT_ROOT/<State name>/**/*.parquet` (for example `Goa/`, `Madhya Pradesh/`, `J&K/`); `Village_boundaries/`
and `India_State_Boundary.zip` are loaded by their own scripts (`src/load_villages.py`, `src/load_state_boundaries.py`).
A file's layer name in the dashboard is its path inside the state folder.

How a search finds its data: the upload is read and the state(s) it touches are found from `cadastral.state_boundaries`
(a state whose boundary is within 1 km of the area counts, for coasts and borders). For each of those states that has
parcels in the catalog the table is queried; a state with none is reported as "no parcel data" (its villages are still
shown). The sync script has to have been run at least once, otherwise every search says the database has no parcel data.

The leftovers of the old on-demand design, `cadastral.state_tables` and `cadastral.load_log`, are not used any more and can be
dropped (`DROP TABLE cadastral.state_tables, cadastral.load_log;`).

## Alignment and Area inputs

The "1. Area of interest" section has two choices.

**Alignment** (a route with chainage) takes three things:

* the **alignment file**: one centreline. Pieces whose ends meet are joined; if the file holds other lines, the longest is used;
* the **right of way (RoW) file**: two lines (the strip between them is searched) or one closed polygon;
* the **start chainage** in metres (up to 3 decimals) and which end of the centreline it belongs to. The ends are marked **A** (first point of
  the line in the file) and **B** (last point) on the map once the alignment is read.

The parcel tables get `chainage from` and `chainage to` (start chainage + distance along the centreline, measured in UTM, 3 decimals) for the part
of each parcel inside the right of way, sorted by increasing chainage. The map shows a chainage mark at the start, the end and every kilometre.

**Area** takes one closed polygon.

Every file can be KML, KMZ, GeoJSON, GeoPackage, GML, a shapefile (zip or loose parts) or DXF. A DXF has no coordinate system, so one is chosen after
the upload (the ones that put the drawing on land in India are listed first). In a DXF the largest closed outline (or filled shape) is the polygon;
a right of way without one is the two longest lines. Elevation is ignored and arcs are kept as drawn.

Code: `backend/app/linear.py` (reading the files), `alignment.centreline()`, `dxf.load_row()`, `chainage.build_linear()`; endpoints
`/api/input/upload`, `/api/input/preview`, and `kind=linear|area` on `/api/query`. The earlier flows (automatic parallel-line finding in an area file,
DXF chainage labels) are no longer reachable from the page; their code is still in the repository.
