"""Converts a stored query result (GeoJSON) into the download formats, for
all sources together, for a single source file, or (GeoPackage only) as one
layer per source."""

import csv
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from fastapi import HTTPException

from . import config, dxf, query

# format ->(file extension served to the user, media type)
FORMATS = {
    "geojson": (".geojson", "application/geo+json"),
    "gpkg": (".gpkg", "application/geopackage+sqlite3"),
    "shp": (".zip", "application/zip"),
    "kml": (".kml", "application/vnd.google-earth.kml+xml"),
    "csv": (".csv", "text/csv"),
}

GDAL_TIMEOUT_S = 600


def _ogr2ogr(args: list[str]) -> None:
    try:
        proc = subprocess.run(["ogr2ogr", *args], capture_output=True, text=True, timeout=GDAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Export took too long.")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise HTTPException(500, f"Export failed: {detail[-1] if detail else 'unknown error'}")


def _parcel_number(props: dict) -> str:
    """The parcel number for one parcel, from the column configured for its source file."""
    column = config.PARCEL_NUMBER_COLUMNS.get(props.get("source", ""))
    candidates = [column] if column else []
    if not column:
        lowered = {k.lower(): k for k in props}
        candidates = [lowered[c] for c in config.PARCEL_NUMBER_FALLBACKS if c in lowered]
    for key in candidates:
        # An original column that clashed with one of the fixed property names is stored as attr_<name>.
        value = props.get(key, props.get(f"attr_{key}"))
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


# The processed (LAP) columns of the parcel table, in this order: (property, column title). Chainage columns only when the
# search had a chainage.
LAP_COLUMNS = [
    ("parcel_number", "Parcel ID"),
    ("area_m2", "Total Area"),
    ("inside_m2", "Affected Area"),
    ("chainage_from", "Chainage From"),
    ("chainage_to", "Chainage To"),
    ("village_name", "Village Name"),
    ("taluka_name", "Taluka Name"),
    ("district_name", "District Name"),
    ("state_name", "State Name"),
]
VILLAGE_LAP_COLUMNS = [
    ("village_name", "Village Name"),
    ("taluka_name", "Taluka Name"),
    ("district_name", "District Name"),
    ("state_name", "State Name"),
]
CHAINAGE_KEYS = ("chainage_from", "chainage_to")


def _lap_columns(has_chainage: bool, columns: list[tuple[str, str]] = LAP_COLUMNS, has_parts: bool = False) -> list[tuple[str, str]]:
    cols = [c for c in columns if has_chainage or c[0] not in CHAINAGE_KEYS]
    if has_parts and columns is LAP_COLUMNS:  # "Part" (1 of 2) right after the parcel id, only when a parcel was cut into pieces
        cols.insert(1, ("part", "Part"))
    return cols


def _has_parts(features: list[dict]) -> bool:
    return any((f["properties"].get("part_count") or 1) > 1 for f in features)


def _has_chainage(features: list[dict]) -> bool:
    return any(f["properties"].get("chainage_from") for f in features)


def _source_props(props: dict) -> dict:
    """The columns of the source file: everything that is not one of the processed (LAP) properties. A source column
    whose name clashed with a LAP property is stored as attr_<name>; it gets its own name back."""
    out = {}
    for key, value in props.items():
        if key in query.INTERNAL_KEYS:
            continue
        if key.startswith("attr_") and key[5:] in query.INTERNAL_KEYS:
            key = key[5:]
        out[key] = value
    return out


def _chain_key(props: dict):
    """Sort key: increasing chainage from, then chainage to; parcels without a chainage last."""
    start = dxf.parse_chainage(str(props.get("chainage_from") or ""))
    if start is None:
        return (1, 0.0, 0.0)
    end = dxf.parse_chainage(str(props.get("chainage_to") or ""))
    return (0, start, start if end is None else end)

def _write_table(path: Path, rows: list[list], header: list[str]) -> None:
    # utf-8-sig so Excel opens non-ASCII text correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _parcel_village(props: dict) -> str:
    """The village name the parcel itself carries (column per source file in config), or "" if none.
    Only a fallback for when the village boundary under the parcel has no name."""
    column = config.PARCEL_VILLAGE_COLUMNS.get(props.get("source", ""))
    if column:
        candidates = [column]
    else:
        lowered = {k.lower(): k for k in props}
        candidates = [lowered[c] for c in config.PARCEL_VILLAGE_FALLBACKS if c in lowered]
    for key in candidates:
        value = props.get(key, props.get(f"attr_{key}"))
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def export_table(result_dir: Path, sources: list[str], source: str | None = None, all_parcels: bool = False, data: str = "lap", combined: bool = False) -> tuple[Path, str, str]:
    """The parcel table: one CSV per source file (or, with `source`, just that one).

    data="lap": the processed columns Parcel ID, Total Area, Affected Area, [Chainage From, Chainage To,] Village Name,
    Taluka Name, District Name, State Name (areas in square metres on the ellipsoid; the state from the state boundary
    file; the village, taluka and district from the village boundary under the parcel). data="source": the columns of
    the source file itself. One row per parcel with some area inside the searched area (parcels that only touch the edge
    are left out); a search along a line lists every parcel it crosses. Rows are in order of increasing chainage when
    there is one, else in layer-priority order."""
    if source is not None and source not in sources:
        raise HTTPException(400, "That source is not part of this result.")
    if data not in ("lap", "source"):
        raise HTTPException(400, "data must be 'lap' or 'source'")
    src = result_dir / "result.geojson"
    if not src.exists():
        raise HTTPException(404, "No such result (it may have expired).")
    # The affected parcels are listed from the parts inside the area: a parcel cut into separate pieces has a row per piece.
    if not all_parcels and (result_dir / "clipped.geojson").exists():
        src = result_dir / "clipped.geojson"

    target = (
        result_dir / f"table_all_{data}.csv"
        if combined
        else result_dir / f"table_{hashlib.sha1(source.encode()).hexdigest()[:10]}_{data}.csv"
        if source
        else result_dir / f"tables_by_source_{data}.zip"
    )
    if combined and not target.exists():
        # One CSV of every parcel of the search. The layer priority was applied while matching (a lower layer only added the
        # parcels no higher layer covers), so this holds one parcel per place, in the order of the layers' priority.
        features = json.loads(src.read_text(encoding="utf-8"))["features"]
        props = [f["properties"] for f in features if f["properties"].get("source") in sources and (all_parcels or f["properties"].get("inside_m2", 0) > 0)]
        if _has_chainage(features):
            props = sorted(props, key=_chain_key)
        if data == "lap":
            columns = _lap_columns(_has_chainage(features), has_parts=_has_parts(features))
            header = [title for _, title in columns]
            rows = [["" if (v := p.get(key)) is None or (key == "inside_m2" and all_parcels) else v for key, _ in columns] for p in props]
        else:
            srcs = [{"source file": p.get("source", "").split("/", 1)[-1], **_source_props(p)} for p in props]
            header = list(dict.fromkeys(k for s in srcs for k in s))
            rows = [["" if s.get(k) is None else s[k] for k in header] for s in srcs]
        _write_table(target, rows, header)
    if not target.exists():
        features = json.loads(src.read_text(encoding="utf-8"))["features"]
        has_chainage = _has_chainage(features)
        columns = _lap_columns(has_chainage, has_parts=_has_parts(features))
        by_source: dict[str, list[dict]] = {name: [] for name in sources}
        for f in features:
            p = f["properties"]
            if p.get("source") in by_source and (all_parcels or p.get("inside_m2", 0) > 0):
                by_source[p["source"]].append(p)

        def table(name: str) -> tuple[list[str], list[list]]:
            props = by_source[name]
            if has_chainage:
                props = sorted(props, key=_chain_key)  # stable: parcels without a chainage keep their order, at the end
            if data == "lap":
                rows = [["" if (v := p.get(key)) is None or (key == "inside_m2" and all_parcels) else v for key, _ in columns] for p in props]
                return [title for _, title in columns], rows
            srcs = [_source_props(p) for p in props]
            header = list(dict.fromkeys(k for s in srcs for k in s))
            return header, [["" if s.get(k) is None else s[k] for k in header] for s in srcs]

        if source:
            header, rows = table(source)
            _write_table(target, rows, header)
        else:
            tmp = result_dir / "tables_tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for name in sources:
                    header, rows = table(name)
                    csv_path = tmp / f"{_table_stem(name)}.csv"
                    _write_table(csv_path, rows, header)
                    zf.write(csv_path, arcname=csv_path.name)
            shutil.rmtree(tmp, ignore_errors=True)

    suffix = "lap" if data == "lap" else "source"
    if combined:
        return target, "text/csv", f"parcels_{suffix}"
    if source:
        return target, "text/csv", f"{_table_stem(source)}_parcel_table_{suffix}"
    return target, "application/zip", f"parcel_tables_by_source_{suffix}"

NO_PARCEL_COLUMNS = [
    ("state", "state"),
    ("district", "district"),
    ("sub_district", "taluka"),
    ("village", "village"),
    ("vill_lgd", "village LGD code"),
    ("subdis_lgd", "taluka LGD code"),
    ("dist_lgd", "district LGD code"),
    ("vill_category", "village category"),
    ("area_m2", "village area (m2)"),
    ("inside_m2", "area inside the searched area (m2)"),
    ("inside_pct", "inside the searched area (%)"),
    ("no_parcels_reason", "reason"),
]


def export_villages_no_parcels(result_dir: Path) -> tuple[Path, str, str]:
    """CSV of the villages found by the search that have no cadastral parcels in the database: one row per
    village with its names, LGD codes, area, and why it has none (no parcel data for its state, or none inside it)."""
    src = result_dir / "villages_no_parcels.geojson"
    if not src.exists():
        raise HTTPException(404, "This search has no villages without parcels (or the result expired).")
    target = result_dir / "villages_without_parcels.csv"
    if not target.exists():
        rows = [f["properties"] for f in json.loads(src.read_text(encoding="utf-8"))["features"]]
        rows.sort(key=lambda p: tuple(str(p.get(k) or "") for k in ("state", "district", "sub_district", "village")))
        with open(target, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow([label for _key, label in NO_PARCEL_COLUMNS])
            for p in rows:
                writer.writerow(["" if p.get(key) is None else p.get(key) for key, _label in NO_PARCEL_COLUMNS])
    return target, "text/csv", "villages_without_parcels"


def _table_stem(source: str) -> str:
    """File name for a source's CSV: the source file name without its extension."""
    return re.sub(r"[^\w.-]+", "_", re.sub(r"\.parquet$", "", source, flags=re.IGNORECASE)).strip("_") or "source"


def layer_name(source: str) -> str:
    """A GIS-friendly layer/file name from a source file name."""
    stem = re.sub(r"\.parquet$", "", source, flags=re.IGNORECASE)
    return re.sub(r"\W+", "_", stem).strip("_").lower()[:60] or "layer"


def _by_source_geojson(src: Path, source: str, result_dir: Path, tag: str) -> Path:
    """A GeoJSON file holding only this source file's features.

    GDAL builds a layer's columns from every feature in the file, so filtering with
    -where alone still gave each per-source export the columns (and clashing types)
    of all the other sources. Filtering first means a source's export has exactly its
    own columns, each with its own type."""
    target = result_dir / f"only_{hashlib.sha1(source.encode()).hexdigest()[:10]}{tag}.geojson"
    if not target.exists():
        features = [f for f in json.loads(src.read_text(encoding="utf-8"))["features"] if f["properties"].get("source") == source]
        target.write_text(
            json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return target


def _projected_geojson(src: Path, data: str, dataset: str, source: str | None, result_dir: Path, tag: str) -> Path:
    """A GeoJSON file with only the LAP columns (data="lap", titled as in the parcel table) or only the source file columns
    (data="source") of each feature, optionally only one source file's, for the CSV export."""
    key = hashlib.sha1((source or "").encode()).hexdigest()[:10]
    target = result_dir / f"csvdata_{dataset}_{data}_{key}{tag}.geojson"
    if not target.exists():
        feats = json.loads(src.read_text(encoding="utf-8"))["features"]
        if source:
            feats = [f for f in feats if f["properties"].get("source") == source]
        if data == "lap":
            cols = _lap_columns(_has_chainage(feats), VILLAGE_LAP_COLUMNS if dataset == "villages" else LAP_COLUMNS, _has_parts(feats))
            out = [{"type": "Feature", "geometry": f["geometry"], "properties": {title: f["properties"].get(k) for k, title in cols}} for f in feats]
        else:
            out = [{"type": "Feature", "geometry": f["geometry"], "properties": _source_props(f["properties"])} for f in feats]
        target.write_text(json.dumps({"type": "FeatureCollection", "features": out}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return target


def _where(source: str) -> list[str]:
    # `source` has already been checked against the result's own source list,
    # so it can only be one of the known file names; quotes are escaped anyway.
    return ["-where", "source = '%s'" % source.replace("'", "''")]


def _convert(fmt: str, src: Path, target: Path, name: str, where: list[str], workdir: Path) -> None:
    common = ["-nln", name, "-a_srs", "EPSG:4326", *where]
    if fmt == "geojson":
        _ogr2ogr(["-f", "GeoJSON", str(target), str(src), *common])
    elif fmt == "gpkg":
        _ogr2ogr(["-f", "GPKG", str(target), str(src), *common, "-nlt", "PROMOTE_TO_MULTI"])
    elif fmt == "kml":
        _ogr2ogr(["-f", "KML", str(target), str(src), *common])
    elif fmt == "csv":
        _ogr2ogr(["-f", "CSV", str(target), str(src), "-lco", "GEOMETRY=AS_WKT", *common])
    elif fmt == "shp":
        shp_dir = workdir / f"shp_{target.stem}"
        shutil.rmtree(shp_dir, ignore_errors=True)
        shp_dir.mkdir()
        _ogr2ogr(
            ["-f", "ESRI Shapefile", str(shp_dir / f"{name}.shp"), str(src), *common,
             "-nlt", "PROMOTE_TO_MULTI", "-lco", "ENCODING=UTF-8"]
        )
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for part in sorted(shp_dir.iterdir()):
                zf.write(part, arcname=part.name)
        shutil.rmtree(shp_dir, ignore_errors=True)


def export(
    result_dir: Path,
    fmt: str,
    sources: list[str],
    source: str | None = None,
    split: bool = False,
    clipped: bool = False,
    dataset: str = "parcels",
    data: str | None = None,
) -> tuple[Path, str, str]:
    """Returns (file path, media type, suggested file stem). Builds the file on
    first request and reuses it after. `sources` is the result's own list of
    source file names, used to validate `source`. With clipped=True each
    parcel is cut to the part inside the area of interest. dataset="villages"
    exports the village boundaries layer instead of the parcels."""
    if fmt not in FORMATS:
        raise HTTPException(400, f"Unknown format '{fmt}'. Use one of: {', '.join(FORMATS)}")
    if source is not None and source not in sources:
        raise HTTPException(400, "That source is not part of this result.")
    if split and fmt != "gpkg":
        raise HTTPException(400, "One layer per source is only available as GeoPackage.")

    suffix, media_type = FORMATS[fmt]
    if dataset not in ("parcels", "villages"):
        raise HTTPException(400, "dataset must be 'parcels' or 'villages'")
    # `base` names the layer inside the file and the file itself.
    base = dataset
    tag = "_clipped" if clipped else ""
    if dataset == "villages":
        src = result_dir / ("villages_clipped.geojson" if clipped else "villages.geojson")
    else:
        src = result_dir / ("clipped.geojson" if clipped else "result.geojson")
    if not src.exists():
        raise HTTPException(404, "No such result (it may have expired).")

    if split:
        target = result_dir / f"parcels_by_source{tag}.gpkg"
        if not target.exists():
            first = True
            for name in sources:
                # Each layer is built from that source's own features only (see _by_source_geojson).
                args = ["-f", "GPKG", str(target), str(_by_source_geojson(src, name, result_dir, tag)), "-nln",
                        layer_name(name), "-a_srs", "EPSG:4326", "-nlt", "PROMOTE_TO_MULTI"]
                if not first:
                    args += ["-update", "-append"]
                _ogr2ogr(args)
                first = False
        return target, media_type, f"parcels_by_source{tag}"

    # CSV can hold either the processed LAP columns or the source file columns.
    if data not in (None, "lap", "source", "all"):
        raise HTTPException(400, "data must be lap or source")
    pick = data if fmt == "csv" and data in ("lap", "source") else None
    dtag = f"_{pick}" if pick else ""

    if source is None:
        if fmt == "geojson":
            return src, media_type, f"{base}{tag}"
        target = result_dir / f"{base}{tag}{dtag}{suffix}"
        if not target.exists():
            _convert(fmt, _projected_geojson(src, pick, dataset, None, result_dir, tag) if pick else src, target, base, [], result_dir)
        return target, media_type, f"{base}{tag}{dtag}"

    key = hashlib.sha1(source.encode()).hexdigest()[:10]
    name = layer_name(source)
    target = result_dir / f"src_{key}{tag}{dtag}{suffix}"
    if not target.exists():
        part = _projected_geojson(src, pick, dataset, source, result_dir, tag) if pick else _by_source_geojson(src, source, result_dir, tag)
        _convert(fmt, part, target, name, [], result_dir)
    return target, media_type, f"{name}{tag}{dtag}"
