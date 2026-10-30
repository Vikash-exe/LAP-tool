import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { FeatureCollection } from "geojson";
import ProgressBar from "./ProgressBar";
import Header from "./Header";
import { previewInput, uploadInput, type DxfCrsOption, type InputInfo, type InputRole, type InputUpload } from "./api";

/** What the search needs from one uploaded file once it is read and its coordinate system (DXF only) is chosen. */
export interface InputSelection {
  token: string;
  crs: string;
}

interface Props {
  role: InputRole;
  title: string;
  /** The info card of this input. */
  info: ReactNode;
  disabled: boolean;
  /** null while the file is not ready (none chosen, still reading, no CRS yet, or an error) */
  onSelection: (selection: InputSelection | null) => void;
  /** What the search would use, to draw on the map; null when nothing is shown. */
  onPreview: (fc: FeatureCollection | null, info: InputInfo | null) => void;
  onFit: (fc: FeatureCollection) => void;
  /** What a good file looks like, shown under the file once it is read. */
  summary: (info: InputInfo) => string;
}

const OTHER = "__other__";
type Stage = "uploading" | "reading" | "drawing" | null;

const label: CSSProperties = { display: "block", fontSize: 12, color: "#666", margin: "8px 0 2px", fontWeight: "bold" };
const small: CSSProperties = { fontSize: 11, color: "#666", marginTop: 3 };
const warn: CSSProperties = { fontSize: 11, color: "#b00020", marginTop: 3 };
const select: CSSProperties = { width: "100%", fontSize: 12 };

function crsLabel(o: DxfCrsOption): string {
  const hint = o.hint ? (o.hint === "defined in the file" ? ` (${o.hint})` : ` (falls in ${o.hint})`) : "";
  return `EPSG:${o.code} · ${o.name}${hint}`;
}

// "other" is only usable once a plausible EPSG code has been typed
function effectiveCrs(choice: string, other: string): string {
  if (choice === OTHER) return /^\d{4,6}$/.test(other.trim()) ? other.trim() : "";
  return choice;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** One file input: choose the file(s), pick the coordinate system for a DXF, see what will be used drawn on the map. */
export default function InputFile({ role, title, info, disabled, onSelection, onPreview, onFit, summary }: Props) {
  const [up, setUp] = useState<InputUpload | null>(null);
  const [stage, setStage] = useState<Stage>(null);
  const [fraction, setFraction] = useState(0);
  const [crs, setCrs] = useState("");
  const [other, setOther] = useState("");
  const [found, setFound] = useState<InputInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);

  const needsCrs = up?.kind === "dxf";
  const crsValue = needsCrs ? effectiveCrs(crs, other) : "none";

  function clear() {
    if (fileInput.current) fileInput.current.value = "";
    void choose([]);
  }

  async function choose(files: File[]) {
    const mine = ++seq.current;
    setUp(null);
    setFound(null);
    setError(null);
    onPreview(null, null);
    onSelection(null);
    if (files.length === 0) return;
    setStage("uploading");
    setFraction(0);
    try {
      const u = await uploadInput(files, (f) => mine === seq.current && setFraction(f), () => mine === seq.current && setStage("reading"));
      if (mine !== seq.current) return;
      setUp(u);
      // the CRS defined in the file, else the first one that puts the drawing on land in India
      const first = u.crs_options.find((o) => o.hint);
      setCrs(first ? String(first.code) : "");
      setOther("");
      if (u.kind === "dxf") setStage(null);
    } catch (e) {
      if (mine === seq.current) {
        setError(message(e));
        setStage(null);
      }
    }
  }

  // Read the file for its role and draw it, whenever the file or the coordinate system changes.
  useEffect(() => {
    if (!up) return;
    if (!crsValue) {
      setFound(null);
      onPreview(null, null);
      onSelection(null);
      return;
    }
    let cancelled = false;
    setStage("drawing");
    setError(null);
    onSelection(null);
    previewInput({ token: up.token, role, crs: needsCrs ? crsValue : "" })
      .then((r) => {
        if (cancelled) return;
        setFound(r.info);
        onPreview(r.features, r.info);
        onFit(r.features);
        onSelection({ token: up.token, crs: needsCrs ? crsValue : "" });
      })
      .catch((e) => {
        if (cancelled) return;
        setFound(null);
        onPreview(null, null);
        setError(message(e));
      })
      .finally(() => {
        if (!cancelled) setStage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [up, crsValue]);

  return (
    <div style={{ marginBottom: 6 }}>
      <Header level="sub" title={title} info={info} />
      <input
        ref={fileInput}
        type="file"
        multiple
        accept=".kml,.kmz,.zip,.geojson,.json,.gpkg,.gml,.shp,.dbf,.shx,.prj,.cpg,.dxf"
        disabled={disabled}
        onChange={(e) => void choose(Array.from(e.target.files ?? []))}
        style={{ fontSize: 12, maxWidth: "100%" }}
      />
      {(up || stage !== null || error) && (
        <button type="button" className="mini" onClick={clear} title="Remove this file and its drawing from the map" style={{ marginLeft: 6 }}>
          Clear
        </button>
      )}
      {stage === "uploading" && <ProgressBar fraction={fraction} label="Uploading the file" />}
      {stage === "reading" && <ProgressBar fraction={1} label="Reading the file..." indeterminate />}
      {stage === "drawing" && <ProgressBar fraction={1} label="Reading the file and drawing it on the map..." indeterminate />}
      {error && <div style={warn}>{error}</div>}
      {up && needsCrs && (
        <>
          <span style={label}>Coordinate system of the DXF</span>
          <select value={crs} onChange={(e) => setCrs(e.target.value)} disabled={disabled} style={select}>
            <option value="">Select the coordinate system...</option>
            {up.crs_options.map((o) => (
              <option key={o.code} value={String(o.code)}>
                {crsLabel(o)}
              </option>
            ))}
            <option value={OTHER}>Other EPSG code...</option>
          </select>
          {crs === OTHER && (
            <input
              type="text"
              inputMode="numeric"
              placeholder="EPSG code, e.g. 32644"
              value={other}
              onChange={(e) => setOther(e.target.value)}
              disabled={disabled}
              style={{ ...select, marginTop: 4 }}
            />
          )}
          {!crsValue && <div style={small}>A DXF does not say which coordinate system it uses. The ones that put the drawing on land in India are listed first.</div>}
        </>
      )}
      {found && <div style={small}>{summary(found)}</div>}
      {found?.warning && <div style={warn}>{found.warning}</div>}
    </div>
  );
}
