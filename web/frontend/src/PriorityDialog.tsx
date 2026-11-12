import { useEffect, useMemo, useRef, useState } from "react";
import { MIN_NEAR, type Footprints, type SourceLayer } from "./api";

interface Props {
  sources: SourceLayer[];
  /** The layer names, highest priority first. */
  order: string[];
  /** The layers that will be loaded and searched (the checked ones). */
  enabled: string[];
  footprints: Footprints | null;
  onChange: (order: string[]) => void;
  onEnabledChange: (enabled: string[]) => void;
  onClose: () => void;
}

const fmt = (n: number) => n.toLocaleString();
const colour = (i: number) => `hsl(${(i * 47) % 360} 72% 48%)`;

/** Every point of a GeoJSON geometry. */
function points(g: unknown, out: [number, number][] = []): [number, number][] {
  if (Array.isArray(g)) {
    if (typeof g[0] === "number") out.push([g[0] as number, g[1] as number]);
    else g.forEach((x) => points(x, out));
  } else if (g && typeof g === "object") {
    const o = g as { coordinates?: unknown; geometries?: unknown[] };
    if (o.coordinates) points(o.coordinates, out);
    o.geometries?.forEach((x) => points(x, out));
  }
  return out;
}

/** A small map of where each layer has parcels (one colour per layer) and where the searched area is (red). */
function FootprintMap({ fp, names, enabled, hover }: { fp: Footprints; names: string[]; enabled: Set<string>; hover: string | null }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const state = fp.states[0];
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || !state) return;
    const W = canvas.width, H = canvas.height;
    const ctx = canvas.getContext("2d")!;
    ctx.clearRect(0, 0, W, H);
    const ready = state.files.filter((f) => f.state === "ready" && f.bbox);
    if (ready.length === 0) return;
    let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
    ready.forEach((f) => {
      x0 = Math.min(x0, f.bbox![0]); y0 = Math.min(y0, f.bbox![1]); x1 = Math.max(x1, f.bbox![2]); y1 = Math.max(y1, f.bbox![3]);
    });
    const kx = Math.cos((((y0 + y1) / 2) * Math.PI) / 180); // a degree of longitude is shorter away from the equator
    const pad = 8;
    const s = Math.min((W - 2 * pad) / ((x1 - x0) * kx || 1), (H - 2 * pad) / (y1 - y0 || 1));
    const px = (x: number) => pad + (x - x0) * kx * s;
    const py = (y: number) => H - pad - (y - y0) * s;
    const draw = (f: (typeof ready)[number], alpha: number, solid: boolean) => {
      const i = names.indexOf(f.name);
      ctx.fillStyle = solid ? colour(i) : colour(i).replace(")", ` / ${alpha})`);
      const c = state.cell;
      f.cells!.forEach(([ix, iy]) => ctx.fillRect(px(ix * c), py((iy + 1) * c), Math.max(1.5, c * kx * s), Math.max(1.5, c * s)));
    };
    ready.filter((f) => !enabled.has(f.name) && f.name !== hover).forEach((f) => { ctx.fillStyle = "rgba(140,150,160,0.35)"; f.cells!.forEach(([ix, iy]) => ctx.fillRect(px(ix * state.cell), py((iy + 1) * state.cell), Math.max(1.5, state.cell * kx * s), Math.max(1.5, state.cell * s))); });
    ready.filter((f) => enabled.has(f.name) && f.name !== hover).forEach((f) => draw(f, 0.55, false));
    const h = ready.find((f) => f.name === hover);
    if (h) draw(h, 1, true);
    // the searched area, red, with a ring so that a short line in a big state can still be found
    const pts = points(fp.area);
    if (pts.length) {
      ctx.strokeStyle = "#d00"; ctx.lineWidth = 2; ctx.beginPath();
      pts.forEach(([x, y], k) => (k ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y))));
      ctx.stroke();
      const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length, cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
      ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(px(cx), py(cy), 9, 0, 2 * Math.PI); ctx.stroke();
    }
    ctx.fillStyle = "#566"; ctx.font = "10px sans-serif";
    ctx.fillText(`${x0.toFixed(1)}°E`, 4, H - 1); ctx.fillText(`${x1.toFixed(1)}°E`, W - 40, H - 1);
    ctx.fillText(`${y1.toFixed(1)}°N`, 4, 10);
  }, [fp, state, names, enabled, hover]);
  return <canvas ref={ref} width={520} height={260} style={{ width: "100%", height: "auto", border: "1px solid #bfe3e5", borderRadius: 8, background: "#f4f9fa" }} />;
}

/** The window to choose which layers to load and search, and to put them in order of priority: the top one is kept whole, the
 * rest only fill in what is not covered above. Unchecked layers are not loaded at all. */
export default function PriorityDialog({ sources, order, enabled, footprints, onChange, onEnabledChange, onClose }: Props) {
  const [hover, setHover] = useState<string | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const byName = new Map(sources.map((s) => [s.source, s]));
  const fpByName = useMemo(() => new Map((footprints?.states ?? []).flatMap((s) => s.files.map((f) => [f.name, f] as const))), [footprints]);
  const names = useMemo(() => sources.map((s) => s.source), [sources]);
  const on = useMemo(() => new Set(enabled), [enabled]);
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= order.length) return;
    const next = [...order];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  const toggle = (name: string) => onEnabledChange(on.has(name) ? enabled.filter((n) => n !== name) : [...enabled, name]);
  const known = footprints != null && footprints.states.every((s) => !s.computing);
  const covering = order.filter((n) => (fpByName.get(n)?.near ?? 0) >= MIN_NEAR);
  const reading = (footprints?.states ?? []).flatMap((s) => s.files).filter((f) => f.state !== "ready" && f.state !== "error");
  const chosen = order.filter((n) => on.has(n)).length;

  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal modal-xl" role="dialog" aria-label="Layers to load and their priority">
        <div className="modal-head">Layers: what to load, and their priority</div>
        <div className="modal-body">
          <p style={{ margin: "0 0 8px", fontSize: 13 }}>
            {sources.length} layers (parquet files) exist for this area, but only the <strong>checked</strong> ones are loaded and searched. The map shows where each has
            parcels; the red mark is your area. Put the layer you trust most at the top: <strong>all its parcels are kept</strong>. From each layer below, only the
            parcels the layers above do not already cover are added, so a layer with nothing here simply gives way to the next one.
          </p>
          {footprints && <FootprintMap fp={footprints} names={names} enabled={on} hover={hover} />}
          {!footprints && <div style={{ fontSize: 12, color: "var(--t2)", margin: "6px 0" }}>Reading where the layers have parcels...</div>}
          {reading.length > 0 && (
            <div style={{ fontSize: 11, color: "var(--t2)", margin: "4px 0" }}>
              Reading the extent of {reading.length} layer{reading.length === 1 ? "" : "s"} (done once, then remembered)...
            </div>
          )}
          <div style={{ display: "flex", gap: 6, margin: "8px 0" }}>
            <button type="button" className="mini" disabled={!known || covering.length === 0} onClick={() => onEnabledChange(covering)} title="Check only the layers that have parcels near your area">
              Only layers covering the area
            </button>
            <button type="button" className="mini" onClick={() => onEnabledChange(order)}>Check all</button>
            <button type="button" className="mini" onClick={() => onEnabledChange([])}>Check none</button>
          </div>
          <ol className="prio-list">
            {order.map((name, i) => {
              const s = byName.get(name);
              const f = fpByName.get(name);
              const idx = names.indexOf(name);
              let where: string;
              if (f?.state === "ready") where = (f.near ?? 0) >= MIN_NEAR ? `about ${fmt(f.near ?? 0)} parcels near the area` : "no parcels near the area";
              else if (f?.state === "error") where = "extent could not be read";
              else where = "reading the extent...";
              return (
                <li key={name} className="prio-row" style={on.has(name) ? undefined : { opacity: 0.6 }} onMouseEnter={() => setHover(name)} onMouseLeave={() => setHover(null)}>
                  <input type="checkbox" checked={on.has(name)} onChange={() => toggle(name)} title="Load and search this layer" />
                  <span style={{ width: 10, height: 10, borderRadius: 2, background: colour(idx), flex: "0 0 auto" }} />
                  <span className="prio-rank">{i + 1}</span>
                  <span className="prio-name">
                    <strong>{name.replace(/\.parquet$/i, "")}</strong>
                    <span className="prio-sub" style={f?.state === "ready" && (f.near ?? 0) < MIN_NEAR ? { color: "var(--warn, #b45f06)" } : undefined}>
                      {where}
                      {s?.parcels != null ? ` · ${fmt(s.parcels)} loaded in the area` : s?.parcels === null && s ? " · not loaded yet" : ""}
                    </span>
                  </span>
                  <button type="button" className="mini" onClick={() => move(i, -1)} disabled={i === 0} title="Higher priority">▲</button>
                  <button type="button" className="mini" onClick={() => move(i, 1)} disabled={i === order.length - 1} title="Lower priority">▼</button>
                </li>
              );
            })}
          </ol>
        </div>
        <div className="modal-foot">
          <button type="button" className="mini" onClick={() => onChange([...order].sort((a, b) => a.localeCompare(b)))} title="Alphabetical order">
            Reset order
          </button>
          <span style={{ flex: 1, fontSize: 12, color: chosen === 0 ? "var(--bad)" : "var(--t2)" }}>
            {chosen === 0 ? "Check at least one layer" : `${chosen} of ${order.length} layers will be loaded`}
          </span>
          <button type="button" className="primary" onClick={onClose}>
            Use these layers
          </button>
        </div>
      </div>
    </div>
  );
}
