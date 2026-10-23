import json
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import aoi, chainage, config, dxf, exports, search, statedata


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Drops expired state tables (and leftovers of an interrupted load) now and every minute after.
    statedata.start_sweeper()
    yield


app = FastAPI(title="Cadastral Parcel Finder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

_RESULT_ID = re.compile(r"^[0-9a-f]{32}$")


def _result_dir(result_id: str) -> Path:
    # result_id comes straight from the URL, so it must not be able to climb out
    # of RESULTS_DIR.
    if not _RESULT_ID.match(result_id):
        raise HTTPException(404, "No such result")
    path = config.RESULTS_DIR / result_id
    if not path.is_dir():
        raise HTTPException(404, "No such result (it may have expired).")
    return path


def _purge_old_results() -> None:
    cutoff = time.time() - config.RESULT_TTL_HOURS * 3600
    if not config.RESULTS_DIR.exists():
        return
    for d in config.RESULTS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


@app.get("/api/health")
def health():
    loaded = statedata.registry()  # raises when the database is unreachable
    return {"ok": True, "loaded_states": len(loaded)}


@app.get("/api/states")
def get_states():
    """Every state with parcel files on the mount, whether its parcels are in the database now, and the
    limits that govern loading."""
    registry = {e["state"]: e for e in statedata.registry()}
    states = []
    for folder, info in statedata.state_folders().items():
        entry = registry.get(folder)
        states.append(
            {
                "state": folder,
                "files": len(info["files"]),
                "parquet_bytes": info["bytes"],
                "est_bytes": int(info["bytes"] * config.DB_SIZE_RATIO),
                "status": entry["status"] if entry else "not_loaded",
            }
        )
    return {
        "states": states,
        "max_features": config.MAX_FEATURES,
        "ttl_seconds": config.STATE_TTL_SECONDS,
        "budget_bytes": int(config.STORAGE_BUDGET_GB * 1e9),
    }


@app.get("/api/state-tables")
def get_state_tables():
    """The states whose parcels are in the database (or being loaded) with size and time until they are dropped."""
    tables = statedata.registry()
    return {
        "tables": tables,
        "ttl_seconds": config.STATE_TTL_SECONDS,
        "budget_bytes": int(config.STORAGE_BUDGET_GB * 1e9),
        "in_use_bytes": statedata.storage_in_use(),
    }


@app.delete("/api/state-tables/{state}")
def drop_state_table(state: str):
    """Drop one state's whole parcel table (the dashboard's clear button)."""
    return statedata.drop_state(state)


@app.delete("/api/state-tables")
def drop_all_state_tables():
    """Drop every loaded state table that no running search is using."""
    return statedata.drop_all()


@app.post("/api/query")
def start_query(
    files: list[UploadFile] | None = File(None),
    mode: str = Form("intersects"),
    states: str = Form(""),
    strip: bool = Form(True),
    alignment_token: str = Form(""),
    crs: str = Form(""),
    align_mode: str = Form("auto"),
    chainage_token: str = Form(""),
    chainage_crs: str = Form(""),
    chainage_field: str = Form("Text"),
):
    """Starts a search and returns a job id; the dashboard polls /api/jobs/{id}. The upload is read here, so a
    wrong file type is reported immediately; everything else (finding the states, loading their parcel data
    if it is not in the database yet, querying) happens in the background."""
    if mode not in config.QUERY_MODES:
        raise HTTPException(400, f"mode must be one of {config.QUERY_MODES}")
    restrict = [s.strip() for s in states.split(",") if s.strip()]

    _purge_old_results()
    result_dir = config.RESULTS_DIR / uuid.uuid4().hex
    upload_dir = result_dir / "upload"
    upload_dir.mkdir(parents=True)
    try:
        if alignment_token:
            # A DXF alignment (uploaded earlier, see /api/dxf/upload), with an optional DXF of chainage labels.
            options = {"alignment": str(dxf.path_for(alignment_token)), "crs": dxf._check_crs(crs), "mode": align_mode, "chainage": None}
            if chainage_token:
                options["chainage"] = {
                    "path": str(dxf.path_for(chainage_token)),
                    "crs": dxf._check_crs(chainage_crs or crs),
                    "field": chainage_field or "Text",
                }
            (upload_dir / "dxf_options.json").write_text(json.dumps(options), encoding="utf-8")
        elif chainage_token:
            raise HTTPException(400, "Chainage needs the alignment DXF as well.")
        elif files:
            aoi.save_upload_parts([(f.filename, f.file) for f in files], upload_dir)
        else:
            raise HTTPException(400, "Choose the area file first.")
    except Exception:
        shutil.rmtree(result_dir, ignore_errors=True)
        raise

    job = search.create_job()
    search.start(job, upload_dir, result_dir, mode, restrict, strip)
    return {"job_id": job["id"]}


@app.post("/api/dxf/upload")
def dxf_upload(file: UploadFile = File(...), role: str = Form("alignment")):
    """Stores a DXF for later steps (preview, search) and returns what the dashboard form needs: the text
    columns with how many values each holds, the first 10 text rows, and the CRS choices (the ones that put
    the drawing on land in India first)."""
    if role not in ("alignment", "chainage"):
        raise HTTPException(400, "role must be 'alignment' or 'chainage'")
    token, path = dxf.save_upload(file.filename, file.file)
    try:
        info = dxf.inspect(path, role)
    except Exception:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise
    return {"token": token, "filename": Path(file.filename or "").name, **info}


@app.post("/api/dxf/preview")
def dxf_preview(
    token: str = Form(...),
    role: str = Form(...),
    crs: str = Form(""),
    mode: str = Form("auto"),
    field: str = Form("Text"),
):
    """The alignment lines that would be searched (the longest line(s); shorter ones are markings), or the
    chainage labels (text starting with CH / KM), in EPSG:4326, so they can be drawn on the map and zoomed to."""
    path = dxf.path_for(token)
    if role == "alignment":
        fc, info = dxf.load_alignment(path, crs, mode)
        return {"features": fc, "info": info}
    if role == "chainage":
        labels, info = dxf.read_chainage(path, crs, field or "Text")
        info["first"] = dxf.format_chainage(info["min"])
        info["last"] = dxf.format_chainage(info["max"])
        return {"features": dxf.labels_geojson(labels), "info": info}
    raise HTTPException(400, "role must be 'alignment' or 'chainage'")


@app.post("/api/preview")
def preview_area(files: list[UploadFile] = File(...)):
    """Reads the uploaded area and returns it as GeoJSON (EPSG:4326) so the dashboard can show it on the map and
    zoom to it before anything is searched. Nothing is kept: the upload is deleted right after it is read."""
    workdir = Path(tempfile.mkdtemp(prefix="lap_preview_"))
    try:
        aoi.save_upload_parts([(f.filename, f.file) for f in files], workdir)
        return {"aoi": aoi.load_aoi(workdir)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = search.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such search (the server may have been restarted).")
    return search.public(job)


@app.get("/api/results/{result_id}")
def get_result(result_id: str):
    return json.loads((_result_dir(result_id) / "meta.json").read_text(encoding="utf-8"))


@app.get("/api/results/{result_id}/features")
def result_features(result_id: str, clipped: bool = False, layer: str = "parcels"):
    """The matched parcels (full geometry), or with clipped=true only the part of each inside the area.
    layer=villages gives the village boundaries instead."""
    if layer not in ("parcels", "villages", "villages_no_parcels", "chainage"):
        raise HTTPException(400, "layer must be 'parcels', 'villages', 'villages_no_parcels' or 'chainage'")
    if layer in ("villages_no_parcels", "chainage"):
        name = layer
    else:
        name = ("villages_clipped" if clipped else "villages") if layer == "villages" else ("clipped" if clipped else "result")
    path = _result_dir(result_id) / f"{name}.geojson"
    if not path.exists():
        raise HTTPException(404, "No such result (it may have expired).")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/results/{result_id}/download")
def download_result(
    result_id: str,
    format: str = "geojson",
    source: str | None = None,
    split: bool = False,
    clipped: bool = False,
    layer: str = "parcels",
):
    result_dir = _result_dir(result_id)
    meta = json.loads((result_dir / "meta.json").read_text(encoding="utf-8"))
    if layer not in ("parcels", "villages", "villages_no_parcels"):
        raise HTTPException(400, "layer must be 'parcels', 'villages' or 'villages_no_parcels'")
    if layer == "villages_no_parcels":
        if format != "csv":
            raise HTTPException(400, "Villages without parcels are available as CSV.")
        if not ((meta.get("villages") or {}).get("no_parcels") or {}).get("count"):
            raise HTTPException(404, "This search found no villages without parcels.")
        path, media_type, stem = exports.export_villages_no_parcels(result_dir)
    elif layer == "villages":
        if format == "table" or source or split:
            raise HTTPException(400, "Village boundaries are one layer: choose a format, optionally clipped.")
        if not (meta.get("villages") or {}).get("count"):
            raise HTTPException(404, "This search found no village boundaries.")
        path, media_type, stem = exports.export(result_dir, format, [], None, False, clipped, dataset="villages")
    elif format == "table":
        # The parcel table CSVs (total area, area affected, parcel number, village name, taluka, district).
        has_chainage = bool((meta.get("chainage") or {}).get("labels_on_alignment"))
        if not meta.get("aoi_area_m2") and not has_chainage:
            raise HTTPException(400, "The parcel table needs a polygon or a strip; lines and points have no area.")
        # One CSV per source file of the searched states, including sources with no parcel inside the
        # area (those get a header-only file), so the set is always complete.
        loaded = [name for folder in meta.get("state_folders", []) for name in statedata.source_files(folder)]
        path, media_type, stem = exports.export_table(
            result_dir, sorted(set(loaded) | set(meta["per_source"])), source or None, all_parcels=not meta.get("aoi_area_m2")
        )
    else:
        path, media_type, stem = exports.export(
            result_dir, format, list(meta["per_source"]), source or None, split, clipped
        )
    return FileResponse(path, media_type=media_type, filename=f"cadastral_{stem}_{result_id[:8]}{path.suffix}")


# The built frontend (web/frontend/dist) is served by the backend itself, so the dashboard is one
# process on one port. This stays last: the /api routes above are matched first.
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
