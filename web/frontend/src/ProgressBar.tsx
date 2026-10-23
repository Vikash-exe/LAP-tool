interface Props {
  fraction: number; // 0..1
  label: string;
  // For the gap between "every byte has left the browser" and "the
  // server finished writing the file and responded" -- for a
  // multi-GB upload that gap is real, and a percentage frozen at 100%
  // for a long stretch reads as broken rather than "still working".
  indeterminate?: boolean;
}

export default function ProgressBar({ fraction, label, indeterminate }: Props) {
  const pct = Math.round(Math.min(1, Math.max(0, fraction)) * 100);
  return (
    <div style={{ margin: "4px 0" }}>
      <div style={{ fontSize: 11, color: "#666", marginBottom: 2 }}>{indeterminate ? label : `${label} ${pct}%`}</div>
      <div style={{ height: 6, background: "#eee", borderRadius: 3, overflow: "hidden", position: "relative" }}>
        {indeterminate ? (
          <div
            style={{
              position: "absolute",
              height: "100%",
              width: "40%",
              background: "#4a90d9",
              borderRadius: 3,
              animation: "progressbar-indeterminate 1.1s ease-in-out infinite",
            }}
          />
        ) : (
          <div style={{ height: "100%", width: `${pct}%`, background: "#4a90d9", transition: "width 0.15s" }} />
        )}
      </div>
      {indeterminate && (
        <style>{`
          @keyframes progressbar-indeterminate {
            0% { left: -40%; }
            100% { left: 100%; }
          }
        `}</style>
      )}
    </div>
  );
}
