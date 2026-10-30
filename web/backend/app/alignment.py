"""Finding the alignment among the lines of an uploaded area file (KML, KMZ, shapefile, GeoJSON, GeoPackage, GML).

A file that holds lines is treated as an alignment, the way a DXF is (dxf.load_alignment), but a file of lines is often a
mess: the two edges of a corridor drawn as hundreds of short pieces with small gaps and overlaps, in any order and
direction, with elevation values, tick marks, stray points and other lines elsewhere. So the alignment is found in steps:

  1. FLATTEN. Every line is split into simple pieces, elevation (and M) is thrown away, and everything is projected to
     plain horizontal metres (UTM). Repeated points and zero-length bits are removed, the pieces are simplified by 0.25 m.
     Isolated points are ignored. From here on only horizontal distances and directions matter, so lines that cross at
     different heights, or points with different elevation at the same place, can never be merged.
  2. JOIN. The ends of pieces are joined when they are within 1 m (touching), or within GAP_M while pointing at each other
     (same direction, small sideways offset); pieces that overlap are trimmed. A crossing line does not join: only ends
     join, and only when the directions fit. Pieces become chains.
  3. FIND THE CORRIDOR. Chains that run side by side, MIN_WIDTH_M to MAX_WIDTH_M apart, form the corridor; groups whose ends
     line up (a break of up to UNSUPPORTED_BRIDGE_M in the drawing) are one corridor. Its chains are pointed the same way and
     stacked across its width; the two stacks with the most length are the two edges (the outermost pair when they are about
     as long as a stack between them, so a centreline between the edges is left out). Everything that does not run
     alongside another chain (ticks, side roads, unrelated lines) is a marking and is ignored.
  4. BRIDGE AND CHOOSE. Within each edge the chains are ordered along the route and joined across larger gaps (up to
     BRIDGE_M where the other edge shows that the corridor goes on). If both edges are broken for longer, the file holds
     separate sections and the longest one is used. Both lines are cut to the stretch where they run side by side.

If nothing runs side by side, the rule is the older one: the longest joined line, or the two longest when the second is at
least half as long as the longest.

A file with any polygon is an area and is searched as drawn; a file with only points too. Lines + points use the lines.
A DXF has its own rule (dxf.py).
"""

import json
import math
from collections import defaultdict

import psycopg
from fastapi import HTTPException

from . import query

# ---- tuning (metres and degrees) ---------------------------------------------------------------------------------
SIMPLIFY_M = 0.25  # pieces are simplified by this much (a quarter of a metre does not matter for parcels)
MIN_PIECE_M = 1.0  # shorter pieces are noise
TOUCH_M = 1.0  # ends this close are one point, whatever their directions
GAP_M = 40.0  # ends further apart than this are not joined in the first pass
OVERLAP_M = 40.0  # pieces overlapping up to this much are trimmed and joined
JOIN_ANGLE = 30.0  # degrees: two ends must point at each other this closely
LATERAL_BASE_M = 1.5  # sideways offset allowed for a join: this much ...
LATERAL_SLOPE = 0.05  # ... plus this fraction of the gap (edges are at least 5 m apart, so this never jumps across)
TANGENT_M = 10.0  # the direction at an end is taken over this length
SAMPLE_M = 40.0  # a line is looked at every this many metres when looking for its partner
MIN_WIDTH_M = 4.0  # closer than this is not a corridor (edges are never closer than 5 m; also keeps a line drawn twice from pairing with itself)
MAX_WIDTH_M = 400.0  # nor are two lines further apart than this (edges are 20-150+ m apart)
PAIR_ANGLE = 30.0  # degrees: lines side by side must run this closely in parallel
ACROSS_ANGLE = 35.0  # degrees: and the way across to the partner is at most this far from perpendicular
MIN_PAIR_SAMPLES = 3  # at least this many samples (120 m) side by side make two chains partners
BRIDGE_M = 5000.0  # a gap in one edge is bridged up to this length if the other edge shows the corridor continues
UNSUPPORTED_BRIDGE_M = 2000.0  # and up to this length even where both edges are missing (longer breaks start a new section)
END_SAMPLES = 5  # the partner side at the end of a chain is read from its first / last this many samples (200 m)
TIE = 0.9  # a stack at least this fraction as long as the longest counts as equally long: the outermost pair then wins

_COS_JOIN = math.cos(math.radians(JOIN_ANGLE))
_COS_PAIR = math.cos(math.radians(PAIR_ANGLE))
_COS_BRIDGE = math.cos(math.radians(45.0))
_SIN_ACROSS = math.sin(math.radians(ACROSS_ANGLE))
_CELL = MAX_WIDTH_M

_FLATTEN_SQL = """
WITH raw AS (
  SELECT (ST_Dump(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(x), 4326)))).geom AS g
  FROM unnest(%(geoms)s::text[]) AS t(x)
), ls AS (
  SELECT ST_RemoveRepeatedPoints(ST_Transform(g, %(srid)s)) AS g
  FROM raw WHERE GeometryType(g) = 'LINESTRING'
)
SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(g, %(simp)s), 2)
FROM ls WHERE ST_NPoints(g) >= 2 AND ST_Length(g) >= %(minlen)s
"""

_BACK_SQL = """
SELECT ST_AsGeoJSON(w, 9), ST_Length(w::geography)
FROM (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(x), %(srid)s), 4326) AS w
      FROM unnest(%(geoms)s::text[]) AS t(x)) s
"""


# ------------------------------------------------------------------------------------------------ small geometry
def _length(c: list) -> float:
    return sum(math.hypot(c[i + 1][0] - c[i][0], c[i + 1][1] - c[i][1]) for i in range(len(c) - 1))


def _end_direction(c: list, at_end: bool) -> tuple[float, float]:
    """Unit vector pointing out of the line at one of its ends (taken over the last TANGENT_M metres)."""
    pts = c[::-1] if at_end else c
    bx, by = pts[0]
    walked = 0.0
    px, py = bx, by
    for x, y in pts[1:]:
        walked += math.hypot(x - px, y - py)
        px, py = x, y
        if walked >= TANGENT_M:
            break
    dx, dy = bx - px, by - py
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d > 0 else (0.0, 0.0)


def _samples(c: list, step: float) -> list[tuple[float, float, float, float]]:
    """(x, y, direction x, direction y) every `step` metres along the line, first at half a step."""
    out = []
    nxt = step / 2
    acc = 0.0
    for i in range(len(c) - 1):
        x0, y0 = c[i]
        x1, y1 = c[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        ux, uy = (x1 - x0) / seg, (y1 - y0) / seg
        while nxt <= acc + seg:
            t = nxt - acc
            out.append((x0 + ux * t, y0 + uy * t, ux, uy))
            nxt += step
        acc += seg
    return out


# ------------------------------------------------------------------------------------------------ 2. joining
def _match_ends(chains: list[list], gap: float, overlap: float, lat_base: float, lat_slope: float, cos_angle: float,
                directed: bool = False, support=None, touch: float = TOUCH_M, continues=None):
    """Which chain ends belong together. Two ends match when they are within `touch`, or when they point at each other
    (opposite outward directions within the angle), the second is `gap` ahead at most (or `overlap` behind) and the sideways
    offset is small. Each end matches at most one other end, best fit first, and never so as to close a ring. With
    directed=True only the END of one chain matches the START of another (chains already point the same way) and `support`
    (a function of the middle of the gap and its length) can refuse a match, and so can `continues` (a function of the two
    chains and ends) when the lines on either side of the gap do not fit together.
    Returns {(chain, end): (chain, end)} and the lengths of the gaps."""
    ends = []  # (chain, is_end, x, y, outward direction)
    for cid, c in enumerate(chains):
        for is_end in (0, 1):
            x, y = c[-1] if is_end else c[0]
            tx, ty = _end_direction(c, bool(is_end))
            ends.append((cid, is_end, x, y, tx, ty))
    reach = max(gap, overlap, touch)
    grid = defaultdict(list)
    for i, e in enumerate(ends):
        grid[(int(e[2] // reach), int(e[3] // reach))].append(i)

    candidates = []
    for i, a in enumerate(ends):
        ax, ay = int(a[2] // reach), int(a[3] // reach)
        for gx in (ax - 1, ax, ax + 1):
            for gy in (ay - 1, ay, ay + 1):
                for j in grid.get((gx, gy), ()):
                    b = ends[j]
                    if b[0] == a[0] or (not directed and j < i):
                        continue
                    if directed and not (a[1] == 1 and b[1] == 0):
                        continue
                    vx, vy = b[2] - a[2], b[3] - a[3]
                    d = math.hypot(vx, vy)
                    if d > reach:
                        continue
                    if d <= touch:
                        candidates.append((d, i, j, d))
                        continue
                    if a[4] * b[4] + a[5] * b[5] > -cos_angle:  # outward directions must oppose
                        continue
                    along_a = vx * a[4] + vy * a[5]
                    along_b = -(vx * b[4] + vy * b[5])
                    if along_a < -overlap or along_b < -overlap or along_a > gap or along_b > gap:
                        continue
                    lateral = max(abs(a[4] * vy - a[5] * vx), abs(b[4] * vy - b[5] * vx))
                    if lateral > lat_base + lat_slope * max(along_a, along_b, 0.0):
                        continue
                    if support is not None and not support((a[2] + b[2]) / 2, (a[3] + b[3]) / 2, d):
                        continue
                    if continues is not None and not continues(a[0], a[1], b[0], b[1]):
                        continue
                    candidates.append((d + 3 * lateral, i, j, d))
    candidates.sort()

    parent = list(range(len(chains)))

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    nbr: dict[tuple[int, int], tuple[int, int]] = {}
    gaps = []
    for _score, i, j, d in candidates:
        a, b = ends[i], ends[j]
        if (a[0], a[1]) in nbr or (b[0], b[1]) in nbr or find(a[0]) == find(b[0]):
            continue
        nbr[(a[0], a[1])] = (b[0], b[1])
        nbr[(b[0], b[1])] = (a[0], a[1])
        parent[find(a[0])] = find(b[0])
        gaps.append(d)
    return nbr, gaps


def _join(chains: list[list], *args, **kwargs) -> tuple[list[list], list[float]]:
    """Joins chains end to end (see _match_ends for the arguments). Where pieces overlap, the tail of the first is trimmed.
    Returns the new chains and the lengths of the gaps closed."""
    nbr, gaps = _match_ends(chains, *args, **kwargs)
    done = [False] * len(chains)
    out = []
    # paths start at a chain whose start is free (so directed chains keep their direction), then at any other free end
    order = [c for c in range(len(chains)) if (c, 0) not in nbr] + [c for c in range(len(chains)) if (c, 0) in nbr]
    for cid in order:
        if done[cid] or ((cid, 0) in nbr and (cid, 1) in nbr):
            continue
        entry = 0 if (cid, 0) not in nbr else 1  # enter the chain at its free end
        path = list(chains[cid]) if entry == 0 else list(reversed(chains[cid]))
        done[cid] = True
        exit_end = 1 - entry
        while (cid, exit_end) in nbr:
            nxt, nxt_end = nbr[(cid, exit_end)]
            seg = list(chains[nxt]) if nxt_end == 0 else list(reversed(chains[nxt]))
            ux, uy = _end_direction(path, True)
            sx, sy = seg[0]
            while len(path) > 2 and (path[-1][0] - sx) * ux + (path[-1][1] - sy) * uy > 0:  # overlap: trim the tail
                path.pop()
            path.extend(seg)
            done[nxt] = True
            cid, exit_end = nxt, 1 - nxt_end
        out.append(path)
    out.extend(list(chains[c]) for c in range(len(chains)) if not done[c])  # rings: kept as they are
    return out, gaps


# ------------------------------------------------------------------------------------------------ 3. the corridor
def _analyse(chains: list[list]):
    """For every sample on every chain, the nearest sample of another chain across the corridor on each side: roughly
    perpendicular to the line, the two running in parallel, MIN_WIDTH_M to MAX_WIDTH_M away.
    Returns the samples per chain, the pair counts / widths / same-direction votes, and for every ordered pair how often
    the partner is on the left / on the right, and near each chain end."""
    smp = [_samples(c, SAMPLE_M) for c in chains]
    grid = defaultdict(list)
    for cid, ss in enumerate(smp):
        for (x, y, tx, ty) in ss:
            grid[(int(x // _CELL), int(y // _CELL))].append((cid, x, y, tx, ty))
    min2, max2 = MIN_WIDTH_M**2, MAX_WIDTH_M**2
    pairs = defaultdict(lambda: [0, [], 0])  # (a, b) -> count, widths, same-direction votes
    rel = defaultdict(lambda: [0, 0])  # (chain, partner) -> samples where the partner is on its left, on its right
    end_side = defaultdict(lambda: [0, 0])  # (chain, 0 = start / 1 = end) -> partners on its left, on its right near that end
    for cid, ss in enumerate(smp):
        for k, (x, y, tx, ty) in enumerate(ss):
            cx, cy = int(x // _CELL), int(y // _CELL)
            best = [None, None]  # the nearest partner on the left and the nearest on the right
            best2 = [1e30, 1e30]
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for (c2, x2, y2, tx2, ty2) in grid.get((gx, gy), ()):
                        if c2 == cid:
                            continue
                        vx, vy = x2 - x, y2 - y
                        d2 = vx * vx + vy * vy
                        if d2 > max2 or d2 < min2:
                            continue
                        s = 0 if tx * vy - ty * vx > 0 else 1
                        if d2 >= best2[s]:
                            continue
                        dot = tx * tx2 + ty * ty2
                        if abs(dot) < _COS_PAIR:
                            continue
                        d = math.sqrt(d2)
                        if abs((vx * tx + vy * ty) / d) > _SIN_ACROSS:
                            continue
                        best[s], best2[s] = (c2, d, dot), d2
            for s in (0, 1):
                if best[s]:
                    c2, d, dot = best[s]
                    rec = pairs[(cid, c2) if cid < c2 else (c2, cid)]
                    rec[0] += 1
                    rec[1].append(d)
                    if dot > 0:
                        rec[2] += 1
                    rel[(cid, c2)][s] += 1
                    if k < END_SAMPLES:
                        end_side[(cid, 0)][s] += 1
                    if k >= len(ss) - END_SAMPLES:
                        end_side[(cid, 1)][s] += 1
    return smp, pairs, rel, end_side


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _find_pair(chains: list[list]):
    """The two edges of the corridor among the chains: (one edge's chains, the other's, stats) or None."""
    if len(chains) < 2:
        return None
    smp, pairs, rel, end_side = _analyse(chains)
    strong = {k: v for k, v in pairs.items() if v[0] >= MIN_PAIR_SAMPLES and v[0] >= 0.1 * min(len(smp[k[0]]), len(smp[k[1]]))}
    if not strong:
        return None
    paired = sorted({c for k in strong for c in k})

    # groups whose ends line up across a break belong to one corridor: link them end to end
    def partner_side(c: int, end: int) -> int:
        """+1 / -1 when the partner line is on the left / right of chain c as seen leaving it at that end, 0 when unknown."""
        left, right = end_side[(c, end)]
        if end == 0:  # leaving at the start means looking backwards: left and right swap
            left, right = right, left
        return 1 if left > right else -1 if right > left else 0

    def continues(i: int, ei: int, j: int, ej: int) -> bool:
        # an edge keeps its partner on the same physical side before and after the break: crossed links are refused
        pa, pb = partner_side(paired[i], ei), partner_side(paired[j], ej)
        return pa == 0 or pb == 0 or pa == -pb

    nbr, _ = _match_ends([chains[c] for c in paired], UNSUPPORTED_BRIDGE_M, OVERLAP_M, 5.0, 0.15, _COS_BRIDGE, continues=continues)
    links = {}
    for (i, ei), (j, ej) in nbr.items():
        if i < j:
            links[(paired[i], paired[j])] = 1 if ei != ej else -1  # +1: the two chains run the same way

    parent = list(range(len(chains)))

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for (a, b) in list(strong) + list(links):
        parent[find(a)] = find(b)
    weight: dict[int, int] = defaultdict(int)
    for (a, b), v in strong.items():
        weight[find(a)] += v[0]
    root = max(weight, key=weight.get)
    members = sorted({c for k in list(strong) + list(links) for c in k if find(c) == root})
    other_sections_m = sorted(
        (round(sum(_length(chains[c]) for c in {c for k in strong for c in k if find(c) == r}) / 2) for r in weight if r != root),
        reverse=True,
    )

    # a spanning tree of the strongest relations decides direction and lateral level: a few metres of pinched corridor
    # cannot overrule hundreds of kilometres of evidence
    edges = [(v[0], a, b, "pair") for (a, b), v in strong.items() if find(a) == root]
    edges += [(0.5, a, b, "link") for (a, b) in links if find(a) == root]
    edges.sort(key=lambda e: -e[0])
    tree_parent = {c: c for c in members}

    def tfind(k):
        while tree_parent[k] != k:
            tree_parent[k] = tree_parent[tree_parent[k]]
            k = tree_parent[k]
        return k

    tree = defaultdict(list)
    for w, a, b, kind in edges:
        if tfind(a) != tfind(b):
            tree_parent[tfind(a)] = tfind(b)
            tree[a].append((b, kind))
            tree[b].append((a, kind))

    def lateral_delta(k: int, m: int, o: dict) -> int:
        """+1 when chain m lies on the left of chain k (both pointed along the route), -1 on the right."""
        lk, rk = rel[(k, m)] if o[k] == 1 else rel[(k, m)][::-1]
        lm, rm = rel[(m, k)] if o[m] == 1 else rel[(m, k)][::-1]
        score = (lk + rm) - (rk + lm)
        return 1 if score > 0 else -1 if score < 0 else 0

    orient: dict[int, int] = {}
    level: dict[int, int] = {}
    for start in sorted(members, key=lambda c: -_length(chains[c])):
        if start in orient:
            continue
        orient[start], level[start] = 1, 0
        stack = [start]
        while stack:
            k = stack.pop()
            for m, kind in tree[k]:
                if m in orient:
                    continue
                if kind == "pair":
                    v = strong[(min(k, m), max(k, m))]
                    orient[m] = orient[k] * (1 if v[2] * 2 >= v[0] else -1)
                    level[m] = level[k] + lateral_delta(k, m, orient)
                else:
                    orient[m] = orient[k] * links[(min(k, m), max(k, m))]
                    level[m] = level[k]
                stack.append(m)

    stacks = defaultdict(list)
    for c in members:
        stacks[level[c]].append(c)
    if len(stacks) < 2:
        return None
    total = {lv: sum(_length(chains[c]) for c in cs) for lv, cs in stacks.items()}
    cands = [(min(total[a], total[b]), abs(a - b), a, b) for a in stacks for b in stacks if a < b]
    top = max(c[0] for c in cands)
    _, _, la, lb = max((c for c in cands if c[0] >= TIE * top), key=lambda c: (c[1], c[0]))  # outermost of the long ones
    oriented = lambda c: chains[c] if orient[c] == 1 else list(reversed(chains[c]))  # noqa: E731
    used = len(stacks[la]) + len(stacks[lb])
    return (
        [oriented(c) for c in stacks[la]],
        [oriented(c) for c in stacks[lb]],
        {"middle": len(members) - used, "other_paired": sum(w for r, w in weight.items() if r != root), "other_sections_m": other_sections_m},
    )


def _grid_of(chains: list[list]):
    grid = defaultdict(list)
    for c in chains:
        for (x, y, _tx, _ty) in _samples(c, SAMPLE_M):
            grid[(int(x // _CELL), int(y // _CELL))].append((x, y))
    return grid


def _near(grid, x: float, y: float) -> bool:
    cx, cy = int(x // _CELL), int(y // _CELL)
    r2 = MAX_WIDTH_M**2
    for gx in (cx - 1, cx, cx + 1):
        for gy in (cy - 1, cy, cy + 1):
            for (px, py) in grid.get((gx, gy), ()):
                if (px - x) ** 2 + (py - y) ** 2 <= r2:
                    return True
    return False


def _trim_to_partner(line: list, partner: list) -> list:
    """The part of `line` between its first and last vertex that has the partner line within MAX_WIDTH_M, so that the two
    edges of the alignment cover the same stretch even where one edge was drawn longer than the other."""
    grid = _grid_of([partner])
    ok = [i for i, (x, y) in enumerate(line) if _near(grid, x, y)]
    if len(ok) < 2:
        return line
    return line[ok[0]: ok[-1] + 1]


# ------------------------------------------------------------------------------------------------ the whole thing
def _scan(geom: dict | None, lines: list[dict], kinds: set[str]) -> None:
    if not geom:
        return
    kind = geom.get("type")
    if kind == "LineString":
        lines.append(geom)
        kinds.add("line")
    elif kind == "MultiLineString":
        lines.extend({"type": "LineString", "coordinates": part} for part in geom["coordinates"])
        kinds.add("line")
    elif kind in ("Polygon", "MultiPolygon"):
        kinds.add("polygon")
    elif kind in ("Point", "MultiPoint"):
        kinds.add("point")
    elif kind == "GeometryCollection":
        for part in geom.get("geometries", []):
            _scan(part, lines, kinds)


def _utm_srid(lines: list[dict]) -> int:
    ends = [c for g in lines for c in (g["coordinates"][0], g["coordinates"][-1])]
    lon = sum(c[0] for c in ends) / len(ends)
    lat = sum(c[1] for c in ends) / len(ends)
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return (32600 if lat >= 0 else 32700) + zone


def find_lines(pieces: list[list]) -> tuple[list[list], dict]:
    """The alignment among flattened line pieces (lists of (x, y) in metres): (its lines, a summary)."""
    chains, _gaps = _join(pieces, GAP_M, OVERLAP_M, LATERAL_BASE_M, LATERAL_SLOPE, _COS_JOIN)
    chains.sort(key=_length, reverse=True)
    info: dict = {"pieces": len(pieces), "lines_found": len(chains), "method": "longest_lines", "width_m": None,
                  "bridged": None, "sections": 1, "warning": None}
    found = _find_pair(chains) if len(chains) >= 2 else None
    if found:
        left_chains, right_chains, extra = found
        sides = []
        bridged_n, bridged_m = 0, 0.0
        for mine, other in ((left_chains, right_chains), (right_chains, left_chains)):
            grid = _grid_of(other)
            merged, gaps = _join(mine, BRIDGE_M, OVERLAP_M, 5.0, 0.15, _COS_BRIDGE, directed=True,
                                 support=lambda x, y, d, g=grid: d <= UNSUPPORTED_BRIDGE_M or _near(g, x, y))
            bridged_n += sum(1 for g in gaps if g > GAP_M)  # whatever was joined by more than the first-pass gap is a bridge
            bridged_m += sum(g for g in gaps if g > GAP_M)
            sides.append(sorted(merged, key=_length, reverse=True))
        # the pair of sections (one per edge) that runs side by side for the longest stretch
        flat = sides[0] + sides[1]
        n0 = len(sides[0])
        _smp, pairs, _rel, _ends = _analyse(flat)
        across = {k: v for k, v in pairs.items() if (k[0] < n0) != (k[1] < n0)}
        if across:
            (a, b), v = max(across.items(), key=lambda kv: kv[1][0])
            best_count = v[0]
            if best_count >= MIN_PAIR_SAMPLES:
                widths = v[1]
                info.update(
                    method="parallel_pair",
                    width_m=[round(_percentile(widths, 0.01), 1), round(_percentile(widths, 0.5), 1), round(_percentile(widths, 0.99), 1)],
                    bridged={"count": bridged_n, "metres": round(bridged_m, 1)},
                    sections=1 + len([1 for kv in across.items() if kv[1][0] >= 0.1 * best_count and kv[0] != (a, b)]),
                    edge_parts_m=[[round(_length(c)) for c in sides[0]], [round(_length(c)) for c in sides[1]]],
                )
                if extra["other_sections_m"]:
                    info["other_sections_m"] = extra["other_sections_m"][:5]
                if extra["other_sections_m"] or info["sections"] > 1:
                    info["warning"] = (
                        "The file holds more than one stretch of lines running side by side (separate sections or another "
                        "corridor). Only the longest one is used; search the others separately."
                    )
                if extra["middle"]:
                    info["middle_lines"] = extra["middle"]
                info["lines_found"] = len(chains)
                info["ignored"] = max(0, len(chains) - len(left_chains) - len(right_chains))
                lo, hi = sorted((a, b))
                return [_trim_to_partner(flat[lo], flat[hi]), _trim_to_partner(flat[hi], flat[lo])], info
    # nothing side by side: the older rule
    lengths = [_length(c) for c in chains]
    n = 2 if len(chains) >= 2 and lengths[1] >= 0.5 * lengths[0] else 1
    if len(chains) > n and lengths[n] >= 0.5 * lengths[n - 1]:
        info["warning"] = (
            f"The next longest line ({lengths[n]:,.0f} m) is almost as long as the ones used. "
            "If the alignment is not what you expect, remove the extra long lines from the file."
        )
    return chains[:n], info


def pick(area: dict) -> tuple[dict, dict | None]:
    """The area as it is (and None) unless the file holds lines and no polygon; then the alignment: a
    FeatureCollection with its two edges (or one centreline), and a summary for the dashboard."""
    lines: list[dict] = []
    kinds: set[str] = set()
    for feature in area.get("features", []):
        _scan(feature.get("geometry"), lines, kinds)
    if "polygon" in kinds or not lines:
        return area, None

    srid = _utm_srid(lines)
    try:
        with query.connect() as conn:
            rows = conn.execute(_FLATTEN_SQL, {"geoms": [json.dumps(g) for g in lines], "srid": srid, "simp": SIMPLIFY_M, "minlen": MIN_PIECE_M}).fetchall()
            pieces = [[(float(x), float(y)) for x, y in json.loads(r[0])["coordinates"]] for r in rows]
            if not pieces:
                return area, None  # only degenerate lines: leave the file as it is
            chosen, info = find_lines(pieces)
            back = conn.execute(
                _BACK_SQL, {"geoms": [json.dumps({"type": "LineString", "coordinates": c}) for c in chosen], "srid": srid}
            ).fetchall()
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while finding the alignment: {str(e).splitlines()[0]}")

    info.setdefault("ignored", max(0, info["lines_found"] - len(chosen)))
    info.update(
        lines_used=len(chosen),
        mode_used="two" if len(chosen) == 2 else "single",
        lengths_m=[round(float(r[1]), 1) for r in back],
        ignored_longest_m=None,
    )
    features = [{"type": "Feature", "geometry": json.loads(r[0]), "properties": {"role": "alignment"}} for r in back]
    return {"type": "FeatureCollection", "features": features}, info


def centreline(area: dict, min_piece_m: float = MIN_PIECE_M) -> tuple[dict, dict]:
    """The one centreline in a file of lines: elevation is ignored, pieces whose ends meet are joined, and the
    longest joined line is the alignment. Returns (FeatureCollection with that one LineString in EPSG:4326, summary)."""
    lines: list[dict] = []
    kinds: set[str] = set()
    for feature in area.get("features", []):
        _scan(feature.get("geometry"), lines, kinds)
    if not lines:
        raise HTTPException(400, "The file holds no line. The alignment must be a line (the centreline).")

    srid = _utm_srid(lines)
    try:
        with query.connect() as conn:
            rows = conn.execute(_FLATTEN_SQL, {"geoms": [json.dumps(g) for g in lines], "srid": srid, "simp": SIMPLIFY_M, "minlen": min_piece_m}).fetchall()
            pieces = [[(float(x), float(y)) for x, y in json.loads(r[0])["coordinates"]] for r in rows]
            if not pieces:
                raise HTTPException(400, "The lines in the file are too short to be an alignment.")
            chains, _gaps = _join(pieces, GAP_M, OVERLAP_M, LATERAL_BASE_M, LATERAL_SLOPE, _COS_JOIN)
            chains.sort(key=_length, reverse=True)
            back = conn.execute(
                _BACK_SQL, {"geoms": [json.dumps({"type": "LineString", "coordinates": chains[0]})], "srid": srid}
            ).fetchall()
    except psycopg.Error as e:
        raise HTTPException(500, f"Database error while reading the alignment: {str(e).splitlines()[0]}")

    lengths = [_length(c) for c in chains]
    info = {
        "pieces": len(pieces),
        "lines_found": len(chains),
        "lengths_m": [round(float(back[0][1]), 1)],
        "ignored": len(chains) - 1,
        "warning": None,
    }
    if len(chains) > 1 and lengths[1] >= 0.5 * lengths[0]:
        info["warning"] = (
            f"The file holds another line ({lengths[1]:,.0f} m) almost as long as the one used ({lengths[0]:,.0f} m). "
            "The alignment must be a single centreline: remove the other lines if this is not the one you mean."
        )
    feature = {"type": "Feature", "geometry": json.loads(back[0][0]), "properties": {"role": "alignment"}}
    return {"type": "FeatureCollection", "features": [feature]}, info
