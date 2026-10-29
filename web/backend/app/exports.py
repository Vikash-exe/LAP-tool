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


TABLE_HEADER = ["total area", "area affected", "parcel number", "village name", "taluka", "district"]


CHAINAGE_HEADER = ["chainage from", "chainage to", "chainage labels inside"]


def _chain_order(row: list):
    """Sort key for the parcel table: increasing chainage from, then chainage to; parcels without a chainage last."""
    start = dxf.parse_chainage(str(row[-3] or ""))
    if start is None:
        return (1, 0.0, 0.0)
    end = dxf.parse_chainage(str(row[-2] or ""))
    return (0, start, start if end is None else end)


def _write_table(path: Path, rows: list[list], header: list[str] = TABLE_HEADER) -> None:
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


def export_table(result_dir: Path, sources: list[str], source: str | None = None, all_parcels: bool = False) -> tuple[Path, str, str]:
    """The parcel table: CSV(s) with the columns total area, area affected,
    parcel number, village name, taluka, district.

    One CSV per source file. With `source` given, that one CSV; otherwise a zip
    holding one CSV for every source in the result (a source with no parcel
    inside the area still gets its header-only file, so the set is always
    complete). Areas are square metres on the ellipsoid. One row per parcel with
    some area inside the searched area; parcels that only touch its edge are left out.
    Village name, taluka (the village's sub-district) and district come from the
    village boundary under a point inside each parcel; an empty cell means none was found.
    """
    if source is not None and source not in sources:
        raise HTTPException(400, "That source is not part of this result.")
    src = result_dir / "result.geojson"
    if not src.exists():
        raise HTTPException(404, "No such result (it may have expired).")

    target = (
        result_dir / f"table_{hashlib.sha1(source.encode()).hexdigest()[:10]}.csv"
        if source
        else result_dir / "tables_by_source.zip"
    )
    # Chainage per parcel (from / to, labels inside) when the search was given chainage labels.
    chain_path = result_dir / "chainage_parcels.json"
    chain = json.loads(chain_path.read_text(encoding="utf-8")) if chain_path.exists() else None
    header = TABLE_HEADER + CHAINAGE_HEADER if chain is not None else TABLE_HEADER
    if not target.exists():
        # Rows first (each with the interior point recorded at search time), then one batched
        # village lookup for them all. The points come from the result file, so this works even
        # after the state's parcel table has been dropped.
        # Each item: (interior point, [area, inside, parcel number], the parcel's own village name as a fallback).
        pending: dict[str, list[tuple[tuple[float, float] | None, list, str]]] = {name: [] for name in sources}
        for feature in json.loads(src.read_text(encoding="utf-8"))["features"]:
            props = feature["properties"]
            # A search along a line (no area) lists every parcel the line crosses; "area affected" stays empty.
            if props.get("source") in pending and (all_parcels or props.get("inside_m2", 0) > 0):
                pt = feature.get("pt")
                pending[props["source"]].append(
                    (
                        tuple(pt) if pt else None,
                        [props["area_m2"], "" if all_parcels else props["inside_m2"], _parcel_number(props)],
                        _parcel_village(props),
                        feature.get("id"),
                    )
                )
        # A single-source CSV only needs that source's parcels looked up.
        needed = [(name, i) for name, rows in pending.items() if not source or name == source for i in range(len(rows))]
        found = query.village_lookup([pending[name][i][0] for name, i in needed])
        places = dict(zip(needed, found))

        def with_place(name: str, i: int, row: list, own_village: str, pid) -> list:
            village, taluka, district = places.get((name, i), (None, None, None))
            # The village boundary is authoritative; the parcel's own village name only fills a gap.
            out = row + [village or own_village or None, taluka, district]
            if chain is not None:
                out += chain.get(str(pid), ["", "", ""])
            return out

        rows_by_source: dict[str, list[list]] = {
            name: [with_place(name, i, row, own, pid) for i, (_pt, row, own, pid) in enumerate(rows)]
            for name, rows in pending.items()
        }
        if chain is not None:
            # in order of increasing chainage (the sort is stable: parcels without one keep their order, at the end)
            for rows in rows_by_source.values():
                rows.sort(key=_chain_order)

        if source:
            _write_table(target, rows_by_source[source], header)
        else:
            tmp = result_dir / "tables_tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, rows in rows_by_source.items():
                    csv_path = tmp / f"{_table_stem(name)}.csv"
                    _write_table(csv_path, rows, header)
                    zf.write(csv_path, arcname=csv_path.name)
            shutil.rmtree(tmp, ignore_errors=True)

    if source:
        return target, "text/csv", f"{_table_stem(source)}_parcel_table"
    return target, "application/zip", "parcel_tables_by_source"


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

    if source is None:
        if fmt == "geojson":
            return src, media_type, f"{base}{tag}"
        target = result_dir / f"{base}{tag}{suffix}"
        if not target.exists():
            _convert(fmt, src, target, base, [], result_dir)
        return target, media_type, f"{base}{tag}"

    key = hashlib.sha1(source.encode()).hexdigest()[:10]
    name = layer_name(source)
    target = result_dir / f"src_{key}{tag}{suffix}"
    if not target.exists():
        _convert(fmt, _by_source_geojson(src, source, result_dir, tag), target, name, [], result_dir)
    return target, media_type, f"{name}{tag}"
