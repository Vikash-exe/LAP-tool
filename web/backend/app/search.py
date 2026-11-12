"""A search as a background job.

The dashboard starts a search and polls the job while it runs:

  reading the upload -> finding the state(s) the area falls in -> querying the parcels and villages in the database
  -> adding names and chainage -> done.

The parcel data is permanent in PostgreSQL (src/sync_parcels.py fills it); nothing is loaded or dropped here.
"""

import json
import shutil
from collections import defaultdict
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import HTTPException

from . import alignment, aoi, cancel, chainage, config, dxf, enrich, linear, pieces, priority, query, statedata

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
    if "status" not in fields:
        cancel.check()  # every progress update is a place where the Stop button takes effect
    job.update(fields, updated=time.time())


def _stopped(job: dict, result_dir: Path) -> None:
    shutil.rmtree(result_dir, ignore_errors=True)
    _set(job, status="cancelled", phase="error", message="Stopped", error="Stopped by you.", error_status=499)


def _empty_result(result_dir: Path) -> dict:
    """Result files and statistics for a search that matched no parcels (no state with parcel data)."""
    for name in ("result.geojson", "clipped.geojson"):
        (result_dir / name).write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    zero = {"count": 0, "area_m2": 0.0, "inside_m2": 0.0}
    return {
        "count": 0, "per_state": {}, "per_source": {},
        "stats": {"total": zero, "per_state": {}, "per_source": {}},
        "clipped_count": 0, "fully_inside": 0, "bbox": None, "seconds": 0.0,
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
            limit=config.MAX_VILLAGES, decimals=6, noun="villages", id_key="village_id", with_point=True,
        )
    except HTTPException as e:
        return {"count": 0, "error": e.detail}
    keep = ("count", "per_state", "stats", "clipped_count", "bbox", "seconds")
    return {k: stats[k] for k in keep}


def _bbox_of(features: list[dict]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def visit(node) -> None:
        if node and isinstance(node[0], (int, float)):
            xs.append(node[0])
            ys.append(node[1])
        else:
            for child in node:
                visit(child)

    for f in features:
        visit((f.get("geometry") or {}).get("coordinates") or [])
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def _villages_without_parcels(result_dir: Path, tables_by_folder: dict[str, str]) -> dict:
    """Among the villages found, those with no cadastral parcel in our database, written to
    villages_no_parcels.geojson (each carrying the reason). Never fails the search: an error is reported
    in the result instead."""
    try:
        features = json.loads((result_dir / "villages.geojson").read_text(encoding="utf-8"))["features"]
        ids_by_state: dict[str, list[int]] = defaultdict(list)
        for f in features:
            ids_by_state[f["properties"]["state"]].append(f["properties"]["village_id"])
        reasons: dict[int, str] = {}
        for state, ids in ids_by_state.items():
            table = tables_by_folder.get(state)
            if table is None:
                # No parcel table was searched for this state: it has no parcel data in the database.
                reason = "no parcel data for this state"
                reasons.update({i: reason for i in ids})
            else:
                reasons.update({i: "no parcel lies inside this village" for i in query.villages_without_parcels(table, ids)})
        kept = []
        for f in features:
            reason = reasons.get(f["properties"]["village_id"])
            if reason:
                f["properties"]["no_parcels_reason"] = reason
                kept.append(f)
        (result_dir / "villages_no_parcels.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        per_state: dict[str, int] = defaultdict(int)
        for f in kept:
            per_state[f["properties"]["state"]] += 1
        return {"count": len(kept), "per_state": dict(per_state), "bbox": _bbox_of(kept)}
    except HTTPException as e:
        return {"count": 0, "error": e.detail}
    except Exception as e:  # a failed side check must not lose the search result
        traceback.print_exc()
        return {"count": 0, "error": f"Could not check the villages for parcels: {e}"}


def run_search(job: dict, upload_dir: Path, result_dir: Path, mode: str, restrict: list[str], strip: bool) -> None:
    """The body of the background thread."""
    cancel.begin(job["id"])
    try:
        _set(job, phase="reading", message="Reading the uploaded file")
        dxf_options = None
        if (upload_dir / "dxf_options.json").exists():
            dxf_options = json.loads((upload_dir / "dxf_options.json").read_text(encoding="utf-8"))
        alignment_info = None
        labels = None
        chainage_read = None
        inputs = None
        if (upload_dir / "input_options.json").exists():
            inputs = json.loads((upload_dir / "input_options.json").read_text(encoding="utf-8"))
        # the layers in order of priority (highest first); fewer than two means no priority is applied
        order = [str(s) for s in (inputs or {}).get("priority") or []]
        has_row = True
        add_centreline = False
        centreline = None  # alignment search: the centreline the chainage runs along
        lines = None
        if inputs:
            if inputs["kind"] == "linear":
                a, r = inputs.get("alignment"), inputs.get("row")
                centre_fc = None
                if a:  # without an alignment file there is no centreline, so no chainage
                    centre_fc, _ = linear.read(a["token"], "alignment", a["crs"])
                    centreline = centre_fc["features"][0]["geometry"]
                # what the parcels are matched against: the right of way, the alignment line, or both
                allowed = ({"row"} if r else set()) | ({"line"} if a else set())
                use = set(inputs.get("match_with") or ["row", "line"]) & allowed or allowed
                add_centreline = use == {"row", "line"}
                if "row" in use and r:
                    area, row_info = linear.read(r["token"], "row", r["crs"])
                    area, lines = linear.to_search_area(area, row_info)
                else:
                    area, has_row = centre_fc, False  # the parcels the centreline itself crosses
            else:
                a = inputs["area"]
                area, _ = linear.read(a["token"], "area", a["crs"])
        elif dxf_options:
            # DXF alignment: its longest line(s) only; optionally the chainage labels of a second DXF.
            area, alignment_info = dxf.load_alignment(Path(dxf_options["alignment"]), dxf_options["crs"], dxf_options["mode"])
            c = dxf_options.get("chainage")
            if c:
                _set(job, message="Reading the chainage DXF")
                labels, chainage_read = dxf.read_chainage(Path(c["path"]), c["crs"], c["field"])
        else:
            area = aoi.load_aoi(upload_dir)
            # A file of lines is an alignment: line pieces are joined (1 m), the longest one or two are kept.
            area, alignment_info = alignment.pick(area)
        (result_dir / "aoi.geojson").write_text(json.dumps(area), encoding="utf-8")
        if not inputs:
            # Exactly two lines are read as the two edges of a strip.
            lines = query.extract_two_lines(area) if strip else None
        prepared = query.prepare_aoi(area, lines)
        if prepared["strip"]:
            # two lines become one closed polygon, which is what is searched, saved and drawn from here on
            area = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": json.loads(prepared["geometry"]), "properties": {"role": "row"}}]}
            (result_dir / "aoi.geojson").write_text(json.dumps(area), encoding="utf-8")
        if add_centreline and centreline:
            prepared["geometry"] = query.add_line(prepared["geometry"], centreline)  # the line may run outside the right of way

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

        # The parcel data is permanent in the database (filled by src/sync_parcels.py); a state has data when the
        # catalog lists layers for it. If the database holds none at all, say so rather than implying the state has none.
        no_data_note = (
            "no parcel data for this state" if statedata.has_data()
            else "the database has no parcel data yet (run src/sync_parcels.py)"
        )
        # the layers (files) searched in each state: the ones the user chose (all of them when none were chosen)
        wanted: dict[str, list[str]] = {}
        for d in detected:
            if d["folder"]:
                names = statedata.source_files(d["folder"])
                wanted[d["folder"]] = [n for n in order if n in names] if order else names
        entries = [
            {"name": d["name"], "folder": d["folder"] if wanted.get(d["folder"]) else None, "inside": d["inside"],
             "status": "ready" if wanted.get(d["folder"]) else "no_data",
             **({} if wanted.get(d["folder"]) else {"note": no_data_note if not d["folder"] else "no layer chosen for this state"})}
            for d in detected
        ]
        _set(job, states=entries)
        folders = list(dict.fromkeys(e["folder"] for e in entries if e["folder"]))
        table_of = statedata.ready_tables(folders)
        folders = [f for f in folders if f in table_of]
        tables = [table_of[f] for f in folders]

        excluded: dict[str, set[int]] = {}
        dropped: dict[str, int] = {}
        if len(order) >= 2 and tables:
            _set(job, phase="searching", message="Applying the layer priority")
            for table in tables:
                excluded[table], gone = priority.excluded_ids(table, prepared["geometry"], order)
                for src, n in gone.items():
                    dropped[src] = dropped.get(src, 0) + n

        _set(job, phase="searching", message="Searching the parcels and villages")
        if tables:
            stats = query.run_query(
                prepared["geometry"], mode, result_dir / "result.geojson", result_dir / "clipped.geojson",
                tables=tables, with_point=True,
                order=order or None, excluded=excluded or None,
            )
        else:
            stats = _empty_result(result_dir)
        villages = _query_villages(prepared["geometry"], mode, restrict, result_dir)
        # village / taluka / district / state of every parcel and village, stored in the result files
        _set(job, message="Adding village, taluka, district and state names")
        enrich.enrich_parcels(result_dir)
        # a parcel the area cuts into separate pieces: one feature, and so one row, per piece
        pieces_count = pieces.split(result_dir)
        if pieces_count is not None:
            stats["clipped_count"] = pieces_count
        if villages and villages.get("count"):
            enrich.enrich_villages(result_dir)
        chainage_meta = None
        if centreline:
            _set(job, message="Working out the chainage of the parcels")
            try:
                chainage_meta = chainage.build_linear(
                    result_dir, centreline, prepared["geometry"], float(inputs["start_chainage"]), inputs["start_end"],
                    float(inputs.get("step") or chainage.DEFAULT_STEP_M),
                )
            except HTTPException as e:
                chainage_meta = {"error": e.detail}
        elif labels:
            # Not allowed to lose the search: a chainage problem is reported in the result instead.
            _set(job, message="Working out the chainage of the parcels")
            try:
                chainage_meta = chainage.build(result_dir, labels, prepared["strip"])
            except HTTPException as e:
                chainage_meta = {"error": e.detail}
        if villages and villages.get("count"):
            _set(job, message="Checking which villages have no parcels")
            villages["no_parcels"] = _villages_without_parcels(result_dir, dict(zip(folders, tables)))

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
            "input_kind": inputs["kind"] if inputs else "legacy",
            "priority": {"order": order, "dropped": dropped, "drop_fraction": priority.DROP_FRACTION} if order else None,
            "aoi_area_m2": round(prepared["area_m2"], 2) if prepared["area_m2"] else None,
            "villages": villages,
            "alignment": alignment_info,
            "chainage": chainage_meta,
            **stats,
        }
        (result_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        shown = area
        if centreline and has_row:  # the right of way and its centreline are drawn together
            shown = {"type": "FeatureCollection", "features": area["features"] + [{"type": "Feature", "geometry": centreline, "properties": {"role": "alignment"}}]}
        _set(job, status="done", phase="done", message="Done", response={**meta, "aoi": shown, "strip": strip_fc})
    except cancel.Cancelled:
        _stopped(job, result_dir)
    except HTTPException as e:
        if cancel.requested():
            _stopped(job, result_dir)
            return
        shutil.rmtree(result_dir, ignore_errors=True)
        _set(job, status="error", phase="error", message="Failed", error=str(e.detail), error_status=e.status_code)
    except Exception as e:  # never let a job thread die silently
        if cancel.requested():  # e.g. the database statement that was cancelled by the Stop button
            _stopped(job, result_dir)
            return
        traceback.print_exc()
        shutil.rmtree(result_dir, ignore_errors=True)
        _set(job, status="error", phase="error", message="Failed", error=f"Unexpected error: {e}", error_status=500)
    finally:
        cancel.end(job["id"])
        shutil.rmtree(upload_dir, ignore_errors=True)


def start(job: dict, upload_dir: Path, result_dir: Path, mode: str, restrict: list[str], strip: bool) -> None:
    threading.Thread(
        target=run_search, args=(job, upload_dir, result_dir, mode, restrict, strip), name=f"search-{job['id'][:8]}", daemon=True
    ).start()
