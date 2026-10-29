import { useEffect, useRef, useState, type ReactNode } from "react";

// Small grey bold text used for the names of the controls.
export const labelStyle = { fontSize: 12, fontWeight: "bold", color: "#666", margin: "4px 0" } as const;

// A section title with a small circular "i" button at its right. Clicking the button opens a card
// with the explanation over the panel, so the panel itself holds only controls and results, not
// paragraphs. The card closes on a click anywhere else or on Escape.
export default function Header({ title, info, level = "section", className }: { title: ReactNode; info?: ReactNode; level?: "page" | "section" | "sub"; className?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const text =
    level === "page" ? (
      <h3 style={{ flex: 1, margin: 0 }}>{title}</h3>
    ) : level === "section" ? (
      <h4 style={{ flex: 1, margin: 0 }}>{title}</h4>
    ) : (
      <div style={{ ...labelStyle, flex: 1, margin: 0 }}>{title}</div>
    );

  return (
    <div
      ref={ref}
      className={className}
      style={className ? { position: "relative" } : { position: "relative", display: "flex", alignItems: "center", gap: 6, margin: level === "sub" ? "4px 0" : "0 0 8px" }}
    >
      {text}
      {info && (
        <button
          type="button"
          className="plain"
          onClick={() => setOpen((o) => !o)}
          aria-label="More information"
          aria-expanded={open}
          title="More information"
          style={{
            width: 18,
            height: 18,
            flexShrink: 0,
            padding: 0,
            borderRadius: "50%",
            border: `1px solid ${open ? "#1976d2" : "#90a4ae"}`,
            background: open ? "#1976d2" : "#fff",
            color: open ? "#fff" : "#546e7a",
            fontFamily: "Georgia, 'Times New Roman', serif",
            fontStyle: "italic",
            fontWeight: "bold",
            fontSize: 12,
            lineHeight: "16px",
            textAlign: "center",
            cursor: "pointer",
          }}
        >
          i
        </button>
      )}
      {open && info && (
        <div
          role="dialog"
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            marginTop: 4,
            zIndex: 30,
            background: "#fff",
            border: "1px solid #cfd8dc",
            borderRadius: 6,
            boxShadow: "0 4px 14px rgba(0,0,0,0.18)",
            padding: "10px 12px",
            fontSize: 12,
            lineHeight: 1.45,
            color: "#333",
            fontWeight: "normal",
            maxHeight: "60vh",
            overflowY: "auto",
          }}
        >
          {info}
        </div>
      )}
    </div>
  );
}

// Paragraph spacing inside an info card.
export function P({ children }: { children: ReactNode }) {
  return <p style={{ margin: "0 0 6px" }}>{children}</p>;
}
