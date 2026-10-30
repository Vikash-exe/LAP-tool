"""Chainage for the matched parcels.

Input: the chainage labels (dxf.read_chainage) and the searched alignment. For every parcel:

  * chainage from / to: where the part of the parcel that lies in the strip (or, for a centreline or a plain
    lines search, the part the line crosses) begins and ends along the alignment, as chainage. The distance along
    the alignment is measured on a centreline in a metric CRS (UTM); the labels are projected onto it, and the
    chainage is interpolated linearly between the two labels around each position (outside the first and last
    label it continues at one chainage metre per metre);
  * labels inside: the labels that physically fall inside the parcel.

Everything is computed in PostGIS (no numpy / shapely on the server); results are written next to the search
result (chainage_parcels.json, chainage.geojson) and picked up by the parcel table CSV.
"""

import bisect
import json
from pathlib import Path

import psycopg
from fastapi import HTTPException

from . import dxf, enrich, query

MAX_OFFSET_M = 500  # a label farther than this from the alignment does not belong to it
BATCH = 300
SIMPLIFY_M = 0.5  # the measuring line is simplified to this tolerance; labels and parcels use the same line

_REFERENCE_SQL = """
WITH l AS (
  SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(a)s), 4326), %(srid)s) AS a,
         CASE WHEN NOT %(has_b)s THEN NULL
              ELSE ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(b)s), 4326), %(srid)s) END AS b
), o AS (
  -- the second edge is turned to run the same way as the first, as the strip search does
  SELECT a, CASE WHEN b IS NULL THEN NULL
                 WHEN ST_Distance(ST_StartPoint(a), ST_StartPoint(b)) + ST_Distance(ST_EndPoint(a), ST_EndPoint(b))
                      <= ST_Distance(ST_StartPoint(a), ST_EndPoint(b)) + ST_Distance(ST_EndPoint(a), ST_StartPoint(b))
                 THEN b ELSE ST_Reverse(b) END AS b
  FROM l
), c AS (
  SELECT CASE WHEN b IS NULL THEN a
         ELSE ST_MakeLine(ARRAY(
                SELECT ST_MakePoint((ST_X(ST_LineInterpolatePoint(a, i::float8 / n)) + ST_X(ST_LineInterpolatePoint(b, i::float8 / n))) / 2,
                                    (ST_Y(ST_LineInterpolatePoint(a, i::float8 / n)) + ST_Y(ST_LineInterpolatePoint(b, i::float8 / n))) / 2)
                FROM generate_series(0, n) AS i ORDER BY i))
         END AS g
  FROM o, LATERAL (SELECT LEAST(30000, GREATEST(200, CEIL(GREATEST(ST_Length(a), COALESCE(ST_Length(b), 0)) / 2)))::int AS n) k
), s AS (
  SELECT ST_SimplifyPreserveTopology(g, %(tol)s) AS g FROM c
)
SELECT ST_AsText(s.g), ST_Length(s.g),
       ST_AsText((SELECT ST_Collect(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(x), 4326), %(srid)s))
                  FROM unnest(%(all)s::text[]) AS u(x)))
FROM s
"""

_LABELS_SQL = """
WITH r AS (SELECT ST_GeomFromText(%(ref)s, %(srid)s) AS g, %(len)s::float8 AS len)
SELECT t.i, ST_LineLocatePoint(r.g, p.g) * r.len, ST_Distance(r.g, p.g)
FROM unnest(%(i)s::int[], %(x)s::float8[], %(y)s::float8[]) AS t(i, x, y)
CROSS JOIN r
CROSS JOIN LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(t.x, t.y), 4326), %(srid)s) AS g) p
"""

_SPANS_SQL = """
WITH r AS (SELECT ST_GeomFromText(%(ref)s, %(srid)s) AS g, %(len)s::float8 AS len),
     s AS (SELECT ST_GeomFromText(%(search)s, %(srid)s) AS g),
     p AS (SELECT t.id, t.full_gj, t.part_gj
           FROM unnest(%(ids)s::bigint[], %(full)s::text[], %(part)s::text[]) AS t(id, full_gj, part_gj))
SELECT p.id, min(q.m), max(q.m)
FROM p CROSS JOIN r CROSS JOIN s
CROSS JOIN LATERAL (
  SELECT ST_LineLocatePoint(r.g, d.geom) * r.len AS m
  FROM ST_DumpPoints(
    CASE WHEN %(strip)s THEN
           CASE WHEN p.part_gj <> '' THEN ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(p.part_gj), 4326), %(srid)s) END
         ELSE ST_Intersection(ST_MakeValid(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(p.full_gj), 4326), %(srid)s)), s.g)
    END) AS d
) q
GROUP BY p.id
"""

_INSIDE_SQL = """
WITH p AS (SELECT t.id, ST_SetSRID(ST_GeomFromGeoJSON(t.full_gj), 4326) AS g
           FROM unnest(%(ids)s::bigint[], %(full)s::text[]) AS t(id, full_gj)),
     l AS (SELECT t.txt, t.val, ST_SetSRID(ST_MakePoint(t.x, t.y), 4326) AS g
           FROM unnest(%(txt)s::text[], %(val)s::float8[], %(x)s::float8[], %(y)s::float8[]) AS t(txt, val, x, y))
SELECT p.id, array_agg(l.txt ORDER BY l.val)
FROM p JOIN l ON ST_Intersects(p.g, l.g)
GROUP BY p.id
"""


def _alignment_lines(aoi: dict, strip_used: bool) -> list[dict]:
    lines: list[dict] = []
    for f in aoi["features"]:
        g = f.get("geometry") or {}
        if g.get("type") == "LineString":
            lines.append(g)
        elif g.get("type") == "MultiLineString":
            lines.extend({"type": "LineString", "coordinates": part} for part in g["coordinates"])
    if strip_used:
        two = query.extract_two_lines(aoi)
        if two:
            return two
    if len(lines) in (1, 2):
        return lines
    raise HTTPException(400, "Chainage needs an alignment of one line (a centreline) or two lines (the corridor's edges).")


def _mapper(ms: list[float], vs: list[float]):
    """Chainage as a function of the distance along the alignment (linear between labels)."""
    sign = 1.0 if vs[-1] >= vs[0] else -1.0

    def chainage(m: float) -> float:
        if m <= ms[0]:
            return vs[0] + sign * (m - ms[0])
        if m >= ms[-1]:
            return vs[-1] + sign * (m - ms[-1])
        i = bisect.bisect_right(ms, m)
        m0, m1, v0, v1 = ms[i - 1], ms[i], vs[i - 1], vs[i]
        return v0 + (v1 - v0) * (m - m0) / (m1 - m0)

    return chainage


def build(result_dir: Path, labels: list[dict], strip_used: bool) -> dict:
    """Computes the chainage of every parcel of a finished search. Raises HTTPException for unusable input."""
    aoi = json.loads((result_dir / "aoi.geojson").read_text(encoding="utf-8"))
    lines = _alignment_lines(aoi, strip_used)
    mean_lon = sum(l["lon"] for l in labels) / len(labels)
    srid = 32600 + int((mean_lon + 180) // 6) + 1  # WGS 84 / UTM north zone of the labels
    a_json = json.dumps(lines[0])
    b_json = json.dumps(lines[1]) if len(lines) > 1 else "{}"  # unused (has_b false) when there is one line

    try:
        with query.connect() as conn:
            ref_wkt, ref_len, search_wkt = conn.execute(
                _REFERENCE_SQL,
                {"a": a_json, "b": b_json, "has_b": len(lines) > 1, "srid": srid, "tol": SIMPLIFY_M, "all": [json.dumps(g) for g in lines]},
            ).fetchone()

            # Where each label lies along the alignment.
            located = conn.execute(
                _LABELS_SQL,
                {"ref": ref_wkt, "srid": srid, "len": ref_len, "i": list(range(len(labels))),
                 "x": [l["lon"] for l in labels], "y": [l["lat"] for l in labels]},
            ).fetchall()
            on_line = sorted(
                ((m, labels[i]) for i, m, off in located if off <= MAX_OFFSET_M),
                key=lambda t: t[0],
            )
            if len(on_line) < 2:
                raise HTTPException(
                    400,
                    f"Fewer than two chainage labels lie within {MAX_OFFSET_M} m of the alignment. "
                    "Check that both DXF files use the coordinate system you selected.",
                )
            # Labels with the same chainage (e.g. a CH and a KM label at one place) count once, at their mean position.
            ms: list[float] = []
            vs: list[float] = []
            for m, lab in on_line:
                if vs and abs(vs[-1] - lab["value"]) < 0.5 and abs(ms[-1] - m) < 50:
                    ms[-1] = (ms[-1] + m) / 2
                else:
                    ms.append(m)
                    vs.append(lab["value"])
            ups = sum(1 for i in range(1, len(vs)) if vs[i] > vs[i - 1])
            downs = sum(1 for i in range(1, len(vs)) if vs[i] < vs[i - 1])
            if len(vs) < 2 or max(ups, downs) < 0.9 * (ups + downs):
                raise HTTPException(
                    400,
                    "The chainage labels do not run in order along the alignment. Check the coordinate systems, "
                    "and that the chainage file belongs to this alignment.",
                )
            chainage_at = _mapper(ms, vs)

            # Parcels.
            result = json.loads((result_dir / "result.geojson").read_text(encoding="utf-8"))["features"]
            parts: dict[int, str] = {}
            clipped_path = result_dir / "clipped.geojson"
            if strip_used and clipped_path.exists():
                for f in json.loads(clipped_path.read_text(encoding="utf-8"))["features"]:
                    parts[f["id"]] = json.dumps(f["geometry"])
            spans: dict[int, tuple[float, float]] = {}
            inside: dict[int, list[str]] = {}
            lab_args = {
                "txt": [l["text"] for l in labels], "val": [l["value"] for l in labels],
                "x": [l["lon"] for l in labels], "y": [l["lat"] for l in labels],
            }
            for start in range(0, len(result), BATCH):
                chunk = result[start : start + BATCH]
                ids = [f["id"] for f in chunk]
                full = [json.dumps(f["geometry"]) for f in chunk]
                part = [parts.get(f["id"], "") for f in chunk]
                for pid, mn, mx in conn.execute(
                    _SPANS_SQL,
                    {"ref": ref_wkt, "srid": srid, "len": ref_len, "search": search_wkt, "strip": strip_used,
                     "ids": ids, "full": full, "part": part},
                ):
                    a, b = chainage_at(mn), chainage_at(mx)
                    spans[pid] = (min(a, b), max(a, b))
                for pid, texts in conn.execute(_INSIDE_SQL, {"ids": ids, "full": full, **lab_args}):
                    inside[pid] = texts
    except psycopg.errors.QueryCanceled:
        raise HTTPException(504, "Working out the chainage of the parcels took too long.")
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while working out the chainage: {str(e).splitlines()[0]}")

    per_parcel = {
        str(f["id"]): [
            dxf.format_chainage(spans[f["id"]][0]) if f["id"] in spans else "",
            dxf.format_chainage(spans[f["id"]][1]) if f["id"] in spans else "",
            "; ".join(inside.get(f["id"], [])),
        ]
        for f in result
    }
    (result_dir / "chainage_parcels.json").write_text(json.dumps(per_parcel), encoding="utf-8")
    used_labels = [lab for _m, lab in on_line]
    (result_dir / "chainage.geojson").write_text(json.dumps(dxf.labels_geojson(used_labels)), encoding="utf-8")
    return {
        "labels": len(labels),
        "labels_on_alignment": len(on_line),
        "labels_far": len(labels) - len(on_line),
        "first": dxf.format_chainage(min(vs)),
        "last": dxf.format_chainage(max(vs)),
        "alignment_length_m": round(ref_len, 1),
        "parcels_with_chainage": len(spans),
        "parcels_with_labels_inside": len(inside),
    }


# ------------------------------------------------------------------------------------------------ linear search
# An alignment search knows the chainage of the start of the centreline, so no labels are needed: the chainage of a
# point is the start chainage plus its distance along the centreline, measured in a metric CRS (UTM).

_LINEAR_REFERENCE_SQL = """
SELECT ST_AsText(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(line)s), 4326), %(srid)s)),
       ST_Length(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(line)s), 4326), %(srid)s)),
       ST_AsText(ST_MakeValid(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(row)s), 4326), %(srid)s)))
"""

_LINEAR_SPANS_SQL = """
WITH r AS (SELECT ST_GeomFromText(%(ref)s, %(srid)s) AS g, %(len)s::float8 AS len),
     s AS (SELECT ST_GeomFromText(%(row)s, %(srid)s) AS g),
     p AS (SELECT t.id, t.gj FROM unnest(%(ids)s::bigint[], %(full)s::text[]) AS t(id, gj))
SELECT p.id, min(q.m), max(q.m)
FROM p CROSS JOIN r CROSS JOIN s
CROSS JOIN LATERAL (
  SELECT ST_LineLocatePoint(r.g, d.geom) * r.len AS m
  FROM ST_DumpPoints(ST_Intersection(ST_MakeValid(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(p.gj), 4326), %(srid)s)), s.g)) AS d
) q
GROUP BY p.id
"""

_TICKS_SQL = """
WITH r AS (SELECT ST_GeomFromText(%(ref)s, %(srid)s) AS g)
SELECT ST_AsGeoJSON(ST_Transform(ST_LineInterpolatePoint(r.g, f), 4326), 7)
FROM r, unnest(%(fractions)s::float8[]) AS t(f)
"""

TICK_FORMAT_DECIMALS = 3


MAX_MARKS = 20000
DEFAULT_STEP_M = 1000.0


def _utm_srid(coords: list) -> int:
    srid = 32600 + int((sum(c[0] for c in coords) / len(coords) + 180) // 6) + 1
    if sum(c[1] for c in coords) < 0:
        srid += 100  # southern hemisphere zones are 327xx
    return srid


def _fmt_chainage(v: float) -> str:
    """Whole metres plain ("CH 12+500"), otherwise with 3 decimals ("CH 12+500.250")."""
    return dxf.format_chainage(v, 0 if abs(v - round(v)) < 1e-9 else TICK_FORMAT_DECIMALS)


def _mark_values(start_m: float, length: float, step: float) -> list[float]:
    """Chainages of the marks: the start, every multiple of the step after it, and the end. Chainage grows from the start."""
    if not step or step <= 0:
        raise HTTPException(400, "The chainage step must be more than 0 metres.")
    if length / step > MAX_MARKS:
        raise HTTPException(
            400,
            f"A step of {step:g} m gives about {int(length / step):,} marks on this {length / 1000:.1f} km alignment. Use a larger step.",
        )
    lo, hi = start_m, start_m + length
    return [lo] + [k * step for k in range(int(lo // step) + 1, int(hi // step) + 1) if lo < k * step < hi] + [hi]


def _mark_points(conn, ref_wkt: str, srid: int, length: float, start_m: float, start_end: str, values: list[float]) -> list[dict]:
    """The marks as point features on the alignment, labelled "CH: 12+100"; the start, the end and every whole kilometre are labelled "KM: 13+000"."""
    fractions = [(t - start_m) / length if start_end == "A" else 1 - (t - start_m) / length for t in values]
    rows = conn.execute(_TICKS_SQL, {"ref": ref_wkt, "srid": srid, "fractions": [min(1.0, max(0.0, f)) for f in fractions]}).fetchall()
    out = []
    last = len(values) - 1
    for i, (t, (g,)) in enumerate(zip(values, rows)):
        ch = _fmt_chainage(t)
        whole_km = abs(t / 1000 - round(t / 1000)) < 1e-9
        prefix = "KM" if i in (0, last) or whole_km else "CH"
        out.append({"type": "Feature", "geometry": json.loads(g), "properties": {"text": f"{prefix}:{ch[2:]}", "chainage_m": t}})
    return out


def marks(centreline: dict, start_m: float, start_end: str, step: float) -> dict:
    """The chainage marks along an alignment as a FeatureCollection (EPSG:4326): no search needed, the start chainage and
    the step are enough."""
    if start_end not in ("A", "B"):
        raise HTTPException(400, "The start of the alignment must be end A or end B.")
    srid = _utm_srid(centreline["coordinates"])
    try:
        with query.connect() as conn:
            ref_wkt, length, _row = conn.execute(
                _LINEAR_REFERENCE_SQL, {"line": json.dumps(centreline), "row": json.dumps(centreline), "srid": srid}
            ).fetchone()
            features = _mark_points(conn, ref_wkt, srid, length, start_m, start_end, _mark_values(start_m, length, step))
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while working out the chainage: {str(e).splitlines()[0]}")
    return {"type": "FeatureCollection", "features": features}


def build_linear(result_dir: Path, centreline: dict, search_geometry: str, start_m: float, start_end: str, step: float = DEFAULT_STEP_M) -> dict:
    """Chainage from / to of every parcel of a finished alignment search, and a mark every step metres.

    centreline: the LineString; search_geometry: the right of way as GeoJSON text; start_m: the chainage in metres at the
    end named by start_end ("A" = first point of the centreline, "B" = last)."""
    if start_end not in ("A", "B"):
        raise HTTPException(400, "The start of the alignment must be end A or end B.")
    coords = centreline["coordinates"]
    srid = _utm_srid(coords)

    def chainage_at(m: float, length: float) -> float:
        return start_m + (m if start_end == "A" else length - m)

    try:
        with query.connect() as conn:
            ref_wkt, length, row_wkt = conn.execute(
                _LINEAR_REFERENCE_SQL, {"line": json.dumps(centreline), "row": search_geometry, "srid": srid}
            ).fetchone()
            result = json.loads((result_dir / "result.geojson").read_text(encoding="utf-8"))["features"]
            spans: dict[int, tuple[float, float]] = {}
            for start in range(0, len(result), BATCH):
                chunk = result[start : start + BATCH]
                for pid, mn, mx in conn.execute(
                    _LINEAR_SPANS_SQL,
                    {"ref": ref_wkt, "row": row_wkt, "srid": srid, "len": length,
                     "ids": [f["id"] for f in chunk], "full": [json.dumps(f["geometry"]) for f in chunk]},
                ):
                    a, b = chainage_at(mn, length), chainage_at(mx, length)
                    spans[pid] = (min(a, b), max(a, b))

            # Each piece of a parcel inside the area has its own chainage (the position in the list is its key).
            clip_path = result_dir / "clipped.geojson"
            clip_fc = json.loads(clip_path.read_text(encoding="utf-8")) if clip_path.exists() else None
            piece_spans: dict[int, tuple[float, float]] = {}
            pieces_list = clip_fc["features"] if clip_fc else []
            for start in range(0, len(pieces_list), BATCH):
                chunk = pieces_list[start : start + BATCH]
                for idx, mn, mx in conn.execute(
                    _LINEAR_SPANS_SQL,
                    {"ref": ref_wkt, "row": row_wkt, "srid": srid, "len": length,
                     "ids": list(range(start, start + len(chunk))), "full": [json.dumps(f["geometry"]) for f in chunk]},
                ):
                    a, b = chainage_at(mn, length), chainage_at(mx, length)
                    piece_spans[idx] = (min(a, b), max(a, b))

            # The chainage marks along the alignment: the start, every multiple of the step, the end.
            lo, hi = start_m, start_m + length
            tick_features = _mark_points(conn, ref_wkt, srid, length, start_m, start_end, _mark_values(start_m, length, step))
    except psycopg.errors.QueryCanceled:
        raise HTTPException(504, "Working out the chainage of the parcels took too long.")
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while working out the chainage: {str(e).splitlines()[0]}")

    fmt = lambda v: dxf.format_chainage(v, TICK_FORMAT_DECIMALS)  # noqa: E731
    per_parcel = {
        str(f["id"]): [fmt(spans[f["id"]][0]), fmt(spans[f["id"]][1])] if f["id"] in spans else ["", ""]
        for f in result
    }
    (result_dir / "chainage_parcels.json").write_text(json.dumps(per_parcel), encoding="utf-8")
    enrich.add_chainage(result_dir, per_parcel)  # chainage from / to also on each parcel, for the map pop-up and the CSVs
    if clip_fc:
        for idx, f in enumerate(clip_fc["features"]):
            if idx in piece_spans:
                f["properties"]["chainage_from"], f["properties"]["chainage_to"] = fmt(piece_spans[idx][0]), fmt(piece_spans[idx][1])
        clip_path.write_text(json.dumps(clip_fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (result_dir / "chainage.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": tick_features}), encoding="utf-8")
    return {
        "kind": "linear",
        "start_m": start_m,
        "start_end": start_end,
        "first": fmt(lo),  # the chainage range, low to high, whichever end the start is at
        "last": fmt(hi),
        "alignment_length_m": round(length, 3),
        "step_m": step,
        "labels_on_alignment": len(tick_features),
        "parcels_with_chainage": len(spans),
    }
