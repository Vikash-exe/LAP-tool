"""Synthetic corridors shaped like the real data: long, winding, edges 20-150+ m apart (never under 5 m), many pieces with
gaps and overlaps in any order and direction, plus markings and decoys. Checked against the known true edges."""
import math, random, sys, time
from collections import defaultdict
sys.path.insert(0, ".")
from backend.app import alignment


def make_route(length_km, seed, start):
    rng = random.Random(seed)
    step = 200.0
    x, y = start
    th = 0.0
    pts = [(x, y)]
    for _ in range(int(length_km * 1000 / step)):
        th += rng.gauss(0, 0.03) - 0.02 * th
        x += step * math.cos(th)
        y += step * math.sin(th)
        pts.append((x, y))
    return pts, step


def width(s, base_phase=1.0, pinch=None):
    w = 20 + 130 * (0.5 + 0.5 * math.sin(2 * math.pi * s / 70000 + base_phase))
    if pinch and pinch[0] <= s <= pinch[1]:
        w = 6.0
    return w


def edges_of(route, step, **kw):
    left, right = [], []
    n = len(route)
    for i, (x, y) in enumerate(route):
        a = route[max(0, i - 1)]
        b = route[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / d, dx / d
        w = width(i * step, **kw)
        left.append((x + nx * w / 2, y + ny * w / 2))
        right.append((x - nx * w / 2, y - ny * w / 2))
    return left, right


def along(poly, step, s0, s1):
    """The part of a polyline (vertices `step` apart) between arc lengths s0 and s1."""
    def at(s):
        i = min(len(poly) - 2, max(0, int(s // step)))
        t = (s - i * step) / step
        return (poly[i][0] + (poly[i + 1][0] - poly[i][0]) * t, poly[i][1] + (poly[i + 1][1] - poly[i][1]) * t)
    out = [at(s0)]
    i = int(s0 // step) + 1
    while i * step < s1 and i < len(poly):
        out.append(poly[i])
        i += 1
    out.append(at(s1))
    return out


def pieces_of(poly, step, rng, missing=(), cut=(1000, 6000), gap=(-3.0, 12.0)):
    total = (len(poly) - 1) * step
    intervals, s = [], 0.0
    for (m0, m1) in sorted(missing):
        if m0 > s:
            intervals.append((s, m0))
        s = max(s, m1)
    intervals.append((s, total))
    out = []
    for (a, b) in intervals:
        cuts, s = [a], a
        while s < b:
            s = min(b, s + rng.uniform(*cut))
            cuts.append(s)
        joints = [0.0] + [rng.uniform(*gap) for _ in range(len(cuts) - 2)] + [0.0]
        for k in range(len(cuts) - 1):
            s0 = cuts[k] + joints[k] / 2
            s1 = cuts[k + 1] - joints[k + 1] / 2
            if s1 - s0 < 2:
                continue
            piece = along(poly, step, max(0, s0), min(total, s1))
            out.append(piece if rng.random() < 0.5 else piece[::-1])
    return out


def dense(poly, spacing=5.0):
    out = []
    for i in range(len(poly) - 1):
        (x0, y0), (x1, y1) = poly[i], poly[i + 1]
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) // spacing))
        out.extend((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n))
    out.append(poly[-1])
    return out


def grid_of(points, cell=20.0):
    g = defaultdict(list)
    for x, y in points:
        g[(int(x // cell), int(y // cell))].append((x, y))
    return g


def near(grid, x, y, tol, cell=20.0):
    cx, cy = int(x // cell), int(y // cell)
    r = int(math.ceil(tol / cell))
    for gx in range(cx - r, cx + r + 1):
        for gy in range(cy - r, cy + r + 1):
            for (px, py) in grid.get((gx, gy), ()):
                if (px - x) ** 2 + (py - y) ** 2 <= tol * tol:
                    return True
    return False


def coverage(sample_poly, target_grid, tol=3.0, every=100.0):
    pts = [p for p in dense(sample_poly, every)]
    return sum(near(target_grid, x, y, tol) for x, y in pts) / len(pts)


def length(p):
    return sum(math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]) for i in range(len(p) - 1))


ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")


def judge(label, result, info, truthL, truthR, expect_len_km, tol_pct=1.0, expect_method="parallel_pair"):
    gL, gR = grid_of(dense(truthL)), grid_of(dense(truthR))
    print(f"--- {label}: {info['method']}, {len(result)} line(s), lengths {[round(length(r)/1000, 1) for r in result]} km, "
          f"{info['pieces']} pieces -> {info['lines_found']} chains, ignored {info['ignored']}, width {info.get('width_m')}, bridged {info.get('bridged')}, sections {info.get('sections')}")
    check(f"{label}: method is {expect_method}", info["method"] == expect_method)
    if len(result) != 2:
        check(f"{label}: two lines", False)
        return
    cl = [(coverage(r, gL), coverage(r, gR)) for r in result]
    one_left = (cl[0][0] > 0.98 and cl[1][1] > 0.98) or (cl[1][0] > 0.98 and cl[0][1] > 0.98)
    check(f"{label}: one line on each true edge", one_left, f"(left/right coverage {[(round(a,3), round(b,3)) for a, b in cl]})")
    mine = [grid_of(dense(r)) for r in result]
    complete = [coverage(truthL, g) for g in mine] + [coverage(truthR, g) for g in mine]
    check(f"{label}: each true edge is covered by its line (>98%)", max(complete[0], complete[1]) > 0.98 and max(complete[2], complete[3]) > 0.98, f"({[round(c, 3) for c in complete]})")
    check(f"{label}: length within {tol_pct}% of {expect_len_km} km", all(abs(length(r) / 1000 - expect_len_km) <= expect_len_km * tol_pct / 100 for r in result))


def add_markings(pieces, route, step, rng, k=60):
    left, right = edges_of(route, step)
    for _ in range(k):  # ticks across the corridor
        s = rng.uniform(1000, (len(route) - 2) * step - 1000)
        i = int(s // step)
        (x, y), (x2, y2) = route[i], route[i + 1]
        d = math.hypot(x2 - x, y2 - y)
        nx, ny = -(y2 - y) / d, (x2 - x) / d
        h = width(s) / 2 + rng.uniform(15, 60)
        pieces.append([(x + nx * h, y + ny * h), (x - nx * h, y - ny * h)])
    for _ in range(12):  # side roads that start on an edge and lead away
        s = rng.uniform(1000, (len(route) - 2) * step - 1000)
        i = int(s // step)
        x, y = left[i]
        ang = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(300, 2000)
        pieces.append([(x, y), (x + r * 0.5 * math.cos(ang), y + r * 0.5 * math.sin(ang)), (x + r * math.cos(ang + 0.2), y + r * math.sin(ang + 0.2))])


def build(seed=1, km=600, missing_left=(), missing_both=(), centre=False, decoys=True, pinch=(300000, 306000)):
    rng = random.Random(seed * 7 + 3)
    route, step = make_route(km, seed, (400000.0, 1900000.0))
    L, R = edges_of(route, step, pinch=pinch)
    pieces = pieces_of(L, step, rng, missing=list(missing_left) + list(missing_both)) + pieces_of(R, step, rng, missing=list(missing_both))
    if centre:
        pieces += pieces_of(route, step, rng, cut=(500, 2000))
    add_markings(pieces, route, step, rng)
    if decoys:
        r2, st2 = make_route(100, seed + 50, (400000.0, 2050000.0))  # another corridor, 100 km, elsewhere
        l2, rr2 = edges_of(r2, st2)
        pieces += pieces_of(l2, st2, rng) + pieces_of(rr2, st2, rng)
        for k, (km2, start) in enumerate(((700, (700000.0, 1500000.0)), (650, (200000.0, 2300000.0)))):  # two long lines, different areas
            r3, st3 = make_route(km2, seed + 70 + k, start)
            pieces += pieces_of(r3, st3, rng, cut=(2000, 9000))
    rng.shuffle(pieces)
    return pieces, L, R, step


t0 = time.time()
pieces, L, R, step = build()
res, info = alignment.find_lines(pieces)
print(f"[{time.time()-t0:.1f}s for {len(pieces)} pieces / {sum(len(p) for p in pieces)} vertices]")
judge("S1 600 km, gaps/overlaps, ticks, side roads, decoys", res, info, L, R, 600 * 1.0, tol_pct=2.0)
ws = info["width_m"]
check("S1: reported widths look like 5..~150 m", ws and 5 <= ws[0] <= 8 and 125 <= ws[2] <= 170, str(ws))

pieces, L, R, step = build(seed=2, missing_left=((100000, 100220), (250000, 250300), (420000, 420150)))
res, info = alignment.find_lines(pieces)
judge("S2 left edge missing 220/300/150 m in three places", res, info, L, R, 600.0, tol_pct=2.0)
check("S2: the missing parts were bridged", info["bridged"] and info["bridged"]["count"] >= 3, str(info["bridged"]))

pieces, L, R, step = build(seed=3, missing_both=((400000, 405000),))
res, info = alignment.find_lines(pieces)
print(f"--- S3 both edges missing 5 km at 400 km: {info['method']}, lengths {[round(length(r)/1000,1) for r in res]} km, sections {info['sections']}, warning: {str(info['warning'])[:80]}")
check("S3: the longest section (400 km) is used and the break is reported", info["method"] == "parallel_pair" and all(abs(length(r) / 1000 - 400) < 8 for r in res) and info["warning"] is not None)

print(f"    S3 detail: edge parts after bridging (m) {info.get('edge_parts_m')}, sections {info['sections']}")

pieces, L, R, step = build(seed=31, missing_both=((200000, 201200),))
res, info = alignment.find_lines(pieces)
judge("S3b both edges missing 1.2 km at the same place", res, info, L, R, 600.0, tol_pct=2.0)
print(f"    bridged {info['bridged']}  edge parts (m) {info.get('edge_parts_m')}  sections {info['sections']}  other {info.get('other_sections_m')}")
import collections
_chains, _g = alignment._join(pieces, alignment.GAP_M, alignment.OVERLAP_M, alignment.LATERAL_BASE_M, alignment.LATERAL_SLOPE, alignment._COS_JOIN)
_chains.sort(key=alignment._length, reverse=True)
_f = alignment._find_pair(_chains)
print("    families after the pair step (m):", [[round(alignment._length(c)) for c in fam] for fam in _f[:2]])
for _i, _fam in enumerate(_f[:2]):
    print("      family", _i, "starts/ends:", [(round(c[0][0]), round(c[0][1]), round(c[-1][0]), round(c[-1][1])) for c in _fam][:6])

# one edge much shorter than the other (the right edge missing its first 100 km): both lines are cut to the shared stretch
pieces, L, R, step = build(seed=32, km=300, decoys=False, missing_both=(), pinch=None)
rng_ = __import__("random").Random(5)
route_, st_ = make_route(300, 32, (400000.0, 1900000.0))
L_, R_ = edges_of(route_, st_)
pcs = pieces_of(L_, st_, rng_) + pieces_of(R_, st_, rng_, missing=[(0, 100000)])
res, info = alignment.find_lines(pcs)
print(f"--- S3c right edge missing its first 100 km: lengths {[round(length(r)/1000,1) for r in res]} km")
check("S3c: both lines cover the same 200 km", info["method"] == "parallel_pair" and all(abs(length(r) / 1000 - 200) < 4 for r in res))

pieces, L, R, step = build(seed=4, centre=True)
res, info = alignment.find_lines(pieces)
judge("S4 a centreline in pieces between the edges", res, info, L, R, 600.0, tol_pct=2.0)
check("S4: the centreline was ignored", info.get("middle_lines", 0) >= 1, str(info.get("middle_lines")))

pieces, L, R, step = build(seed=5, km=50, decoys=False, pinch=None)
res, info = alignment.find_lines(pieces)
judge("S5 plain 50 km corridor", res, info, L, R, 50.0, tol_pct=2.0)

# S6: a hairpin, the two legs of the corridor only 300 m apart (lines of the other leg are NOT partners)
step6 = 200.0
route6 = [(400000.0 + i * step6, 1900000.0) for i in range(300)]  # 60 km east
route6 += [(460000.0 + 100, 1900000.0 + 75), (460000.0 + 150, 1900000.0 + 150), (460000.0 + 100, 1900000.0 + 225)]
route6 += [(460000.0 - i * step6, 1900300.0) for i in range(0, 300)]  # 60 km back west, 300 m north
L6, R6 = edges_of(route6, step6, pinch=None)
rng6 = random.Random(61)
pcs6 = pieces_of(L6, step6, rng6) + pieces_of(R6, step6, rng6)
rng6.shuffle(pcs6)
res, info = alignment.find_lines(pcs6)
total6 = (len(route6) - 1) * step6 / 1000
judge("S6 hairpin: legs 300 m apart", res, info, L6, R6, total6, tol_pct=3.0)

# S7: speed with a dense drawing (a vertex every 5 m on a 600 km route)
t7 = time.time()
pieces, L, R, step = build(seed=1)
dense_pieces = [dense(p, 5.0) for p in pieces]
n7 = sum(len(p) for p in dense_pieces)
t7b = time.time()
res, info = alignment.find_lines(dense_pieces)
print(f"--- S7 dense drawing: {n7} vertices, {len(dense_pieces)} pieces, find_lines took {time.time()-t7b:.1f}s")
check("S7: same answer as with sparse vertices", info["method"] == "parallel_pair" and all(abs(length(r) / 1000 - 600) < 12 for r in res))
check("S7: fast enough (< 120 s)", time.time() - t7b < 120)

# two whole lines, and one line: the older rule still holds
res, info = alignment.find_lines([[(0, 0), (5000, 0)], [(0, 60), (5000, 60)]])
check("two whole parallel lines 60 m apart", info["method"] == "parallel_pair" and len(res) == 2 and info["width_m"][1] == 60.0, str(info["width_m"]))
res, info = alignment.find_lines([[(0, 0), (5000, 0)], [(0, 3000), (4000, 3000)]])
check("two long lines 3 km apart are NOT a corridor: the older rule applies", info["method"] == "longest_lines" and len(res) == 2)
res, info = alignment.find_lines([[(0, 0), (5000, 0)], [(0, 3000), (1000, 3000)]])
check("one long line + one short far away -> single line", info["method"] == "longest_lines" and len(res) == 1)
res, info = alignment.find_lines([[(0, 0), (5000, 0)], [(0, 40), (5000, 40)], [(0, 80), (5000, 80)]])
check("three parallel lines: the outer two, the middle one ignored", info["method"] == "parallel_pair" and info.get("middle_lines") == 1 and abs(info["width_m"][1] - 80) < 1, str(info.get("width_m")))
res, info = alignment.find_lines([[(0, 0), (5000, 0)], [(0, 3.0), (5000, 3.0)]])
check("two lines only 3 m apart (closer than 5 m) are not paired", info["method"] == "longest_lines", info["method"])

print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
