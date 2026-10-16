"""A search as a background job.

A search used to be one request. Now it may first have to load a state's parcel files into
the database (minutes for a big state), so it runs as a job that the dashboard polls:

  reading the upload -> finding the state(s) the area falls in -> loading the parcel data of
  those states that is not in the database yet -> querying parcels and villages -> done.

Only the states the area touches are loaded; their tables are dropped again after
config.STATE_TTL_SECONDS without use (statedata.py).
"""

import json
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import HTTPException

from . import aoi, config, loader, query, statedata

JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()
JOB_KEEP_SECONDS = 24 * 3600


def create_job() -> dict:
    now = time.time()
    job = {
        "id": uuid.uuid4().hex,
        "status": "running",  # running | done | error
        "phase": "queued",
        "message": "Starting",
        "states": [],  # one entry per state the area touches, with its load progress
        "error": None,
        "error_status": None,
        "response": None,  # the finished search (same shape the dashboard always received)
        "started": now,
        "updated": now,
    }
    with _jobs_lock:
        for old in [k for k, j in JOBS.items() if now - j["updated"] > JOB_KEEP_SECONDS]:
            del JOBS[old]
        JOBS[job["id"]] = job
    return job


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


def public(job: dict) -> dict:
    """The job as the dashboard sees it."""
    return {
        "id": job["id"],
        "status": job["status"],
        "phase": job["phase"],
        "message": job["message"],
        "states": job["states"],
        "error": job["error"],
        "error_status": job["error_status"],
        "response": job["response"],
        "elapsed": round(time.time() - job["started"], 1),
    }


def _set(job: dict, **fields) -> None:
    job.update(fields, updated=time.time())


def _empty_result(result_dir: Path) -> dict:
    """Result files and statistics for a search that matched no parcels (no state with parcel data)."""
    for name in ("result.geojson", "clipped.geojson"):
        (result_dir / name).write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    zero = {"count": 0, "area_m2": 0.0, "inside_m2": 0.0}
    return {
        "count": 0, "per_state": {}, "per_source": {},
        "stats": {"total": zero, "per_state": {}, "per_source": {}},
        "clipped_count": 0, "bbox": None, "seconds": 0.0,
    }


def _query_villages(aoi_geometry: str, mode: str, restrict: list[str] | None, result_dir: Path) -> dict | None:
    """The village boundaries that match the same area, kept as a separate layer.

    Returns None when no village data is loaded. A village search that is too big
    does not fail the whole request: the parcels are still returned and the
    village layer reports why it is empty."""
    if not query.table_exists(config.VILLAGES_TABLE):
        return None
    try:
        stats = query.run_query(
            aoi_geometry, mode, result_dir / "villages.geojson", result_dir / "villages_clipped.geojson",
            tables=[config.VILLAGES_TABLE], states=restrict or None,
            limit=config.MAX_VILLAGES, decimals=6, noun="villages", id_key="village_id",
        )
    except HTTPException as e:
        return {"count": 0, "error": e.detail}
    keep = ("count", "per_state", "stats", "clipped_count", "bbox", "seconds")
    return {k: stats[k] for k in keep}


def run_search(job: dict, upload_dir: Path, result_dir: Path, mode: str, restrict: list[str], strip: bool) -> None:
    """The body of the background thread."""
    try:
        _set(job, phase="reading", message="Reading the uploaded file")
        area = aoi.load_aoi(upload_dir)
        (result_dir / "aoi.geojson").write_text(json.dumps(area), encoding="utf-8")
        # Exactly two lines are read as the two edges of a strip.
        lines = query.extract_two_lines(area) if strip else None
        prepared = query.prepare_aoi(area, lines)

        _set(job, phase="locating", message="Finding the state(s) the area falls in")
        detected = statedata.detect_states(prepared["geometry"])
        if not detected:
            raise HTTPException(
                400,
                "The area lies outside every state boundary (for example out at sea), so there is no "
                "parcel data to load. Check the area, or move it onto land.",
            )
        if restrict:
            detected = [d for d in detected if d["folder"] in restrict]
            if not detected:
                raise HTTPException(400, "The area does not fall in any of the states you restricted the search to.")

        # A state without a parquet folder or a loaded table has no parcel data. If this server has no parcel
        # data folder at all (not mounted yet), say so, rather than implying the state itself has none.
        no_data_note = (
            "no parcel data for this state" if statedata.mount_available()
            else "the parcel data folder is not available on this server (not mounted or copied here yet)"
        )
        entries = [
            {"name": d["name"], "folder": d["folder"], "inside": d["inside"],
             "status": "queued" if d["folder"] else "no_data",
             **({} if d["folder"] else {"note": no_data_note})}
            for d in detected
        ]
        _set(job, states=entries)
        folders = list(dict.fromkeys(e["folder"] for e in entries if e["folder"]))

        def on_progress(**info) -> None:
            for e in entries:
                if e["folder"] == info.get("state"):
                    e.update({k: v for k, v in info.items() if k != "state"})
                    if info.get("status") == "loading":
                        e["loaded_now"] = True
            _set(job)

        tables: list[str] = []
        with statedata.using(folders):
            for folder in folders:
                _set(job, phase="loading", message=f"Loading parcel data for {folder}")
                tables.append(loader.ensure_loaded(folder, on_progress))

            _set(job, phase="searching", message="Searching the parcels and villages")
            if tables:
                stats = query.run_query(
                    prepared["geometry"], mode, result_dir / "result.geojson", result_dir / "clipped.geojson",
                    tables=tables, with_point=True,
                )
            else:
                stats = _empty_result(result_dir)
            villages = _query_villages(prepared["geometry"], mode, restrict, result_dir)
            # The work is done: the clock for dropping these tables starts now.
            statedata.touch(folders)

        _set(job, phase="finishing", message="Preparing the result")
        strip_fc = (
            {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": json.loads(prepared["geometry"]), "properties": {}}]}
            if prepared["strip"]
            else None
        )
        result_id = result_dir.name
        meta = {
            "result_id": result_id,
            "mode": mode,
            "states": restrict,
            "state_folders": folders,
            "detected_states": entries,
            "strip_used": prepared["strip"],
            "aoi_area_m2": round(prepared["area_m2"], 2) if prepared["area_m2"] else None,
            "villages": villages,
            **stats,
        }
        (result_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        _set(job, status="done", phase="done", message="Done", response={**meta, "aoi": area, "strip": strip_fc})
    except HTTPException as e:
        shutil.rmtree(result_dir, ignore_errors=True)
        _set(job, status="error", phase="error", message="Failed", error=str(e.detail), error_status=e.status_code)
    except Exception as e:  # never let a job thread die silently
        traceback.print_exc()
        shutil.rmtree(result_dir, ignore_errors=True)
        _set(job, status="error", phase="error", message="Failed", error=f"Unexpected error: {e}", error_status=500)
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


def start(job: dict, upload_dir: Path, result_dir: Path, mode: str, restrict: list[str], strip: bool) -> None:
    threading.Thread(
        target=run_search, args=(job, upload_dir, result_dir, mode, restrict, strip), name=f"search-{job['id'][:8]}", daemon=True
    ).start()
