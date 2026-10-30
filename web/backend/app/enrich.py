"""The LAP columns of a finished search.

Right after the parcels and villages are matched, every one of them gets the columns the dashboard and the CSVs show:

  parcel:  parcel_number, village_name, taluka_name, district_name, state_name (+ chainage_from / chainage_to after an
           alignment search, see add_chainage)
  village: village_name, taluka_name, district_name, state_name

The village, taluka and district of a parcel come from the village boundary under a point inside the parcel; the state
name from the state boundary file. They are stored in the result files, so the map pop-ups and the downloads need no
further look-ups (and still work after the state's parcel table has been dropped). Everything else on a feature is
a source file attribute (see query.INTERNAL_KEYS).
"""

import json
from pathlib import Path

from . import exports, query


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write(path: Path, fc: dict) -> None:
    path.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _key(f: dict, id_key: str) -> tuple:
    return (f["properties"].get("state"), f.get("id", f["properties"].get(id_key)))


def _update(result_dir: Path, names: tuple[str, ...], extra: dict[tuple, dict], id_key: str) -> None:
    """Adds the extra properties to the features of the full and the clipped file (matched by state and id)."""
    for name in names:
        fc = _read(result_dir / name)
        if not fc:
            continue
        for f in fc["features"]:
            f["properties"].update(extra.get(_key(f, id_key), {}))
        _write(result_dir / name, fc)


def enrich_parcels(result_dir: Path) -> None:
    fc = _read(result_dir / "result.geojson")
    if not fc or not fc["features"]:
        return
    feats = fc["features"]
    points = [tuple(f["pt"]) if f.get("pt") else None for f in feats]
    villages = query.village_lookup(points)
    states = query.state_lookup(points)
    extra: dict[tuple, dict] = {}
    for f, (village, taluka, district), state in zip(feats, villages, states):
        p = f["properties"]
        extra[_key(f, "parcel_id")] = {
            "parcel_number": exports._parcel_number(p),
            # the village boundary is authoritative; the parcel's own village name only fills a gap
            "village_name": village or exports._parcel_village(p) or None,
            "taluka_name": taluka,
            "district_name": district,
            "state_name": state or p.get("state"),
        }
    _update(result_dir, ("result.geojson", "clipped.geojson"), extra, "parcel_id")


def enrich_villages(result_dir: Path) -> None:
    fc = _read(result_dir / "villages.geojson")
    if not fc or not fc["features"]:
        return
    feats = fc["features"]
    states = query.state_lookup([tuple(f["pt"]) if f.get("pt") else None for f in feats])
    extra: dict[tuple, dict] = {}
    for f, state in zip(feats, states):
        p = f["properties"]
        extra[_key(f, "village_id")] = {
            "village_name": p.get("village"),
            "taluka_name": p.get("sub_district"),
            "district_name": p.get("district"),
            "state_name": state or p.get("state"),
        }
    _update(result_dir, ("villages.geojson", "villages_clipped.geojson"), extra, "village_id")


def add_chainage(result_dir: Path, per_parcel: dict[str, list[str]]) -> None:
    """per_parcel: {parcel id: [chainage from, chainage to, ...]} as chainage.build_linear writes it."""
    for name in ("result.geojson", "clipped.geojson"):
        fc = _read(result_dir / name)
        if not fc:
            continue
        for f in fc["features"]:
            row = per_parcel.get(str(f.get("id")))
            if row:
                f["properties"]["chainage_from"] = row[0] or None
                f["properties"]["chainage_to"] = row[1] or None
        _write(result_dir / name, fc)
