import { useEffect } from "react";
import type { SourceLayer } from "./api";

interface Props {
  sources: SourceLayer[];
  /** The layer names, highest priority first. */
  order: string[];
  onChange: (order: string[]) => void;
  onClose: () => void;
}

const fmt = (n: number) => n.toLocaleString();

/** A small window to put the layers in order of priority: the top one is kept whole, the rest only fill in what is not covered above. */
export default function PriorityDialog({ sources, order, onChange, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const byName = new Map(sources.map((s) => [s.source, s]));
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= order.length) return;
    const next = [...order];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Layer priority">
        <div className="modal-head">Layer priority</div>
        <div className="modal-body">
          <p style={{ margin: "0 0 8px", fontSize: 13 }}>
            {sources.length} layers are available here. Put the layer you trust most at the top: <strong>all its parcels are kept</strong>. From each layer below,
            only the parcels that the layers above do not already cover (less than half of the parcel under them) are added.
          </p>
          <ol className="prio-list">
            {order.map((name, i) => {
              const s = byName.get(name);
              return (
                <li key={name} className="prio-row">
                  <span className="prio-rank">{i + 1}</span>
                  <span className="prio-name">
                    <strong>{name.replace(/\.parquet$/i, "")}</strong>
                    <span className="prio-sub">
                      {s?.states.join(", ")}
                      {s?.parcels != null ? ` · ${fmt(s.parcels)} parcels in the area` : ""}
                    </span>
                  </span>
                  <button type="button" className="mini" onClick={() => move(i, -1)} disabled={i === 0} title="Higher priority">
                    ▲
                  </button>
                  <button type="button" className="mini" onClick={() => move(i, 1)} disabled={i === order.length - 1} title="Lower priority">
                    ▼
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
        <div className="modal-foot">
          <button type="button" className="mini" onClick={() => onChange(sources.map((s) => s.source))} title="Alphabetical order">
            Reset order
          </button>
          <span style={{ flex: 1 }} />
          <button type="button" className="primary" onClick={onClose}>
            Use this order
          </button>
        </div>
      </div>
    </div>
  );
}
