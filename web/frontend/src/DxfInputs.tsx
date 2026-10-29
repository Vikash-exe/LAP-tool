import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { FeatureCollection } from "geojson";
import ProgressBar from "./ProgressBar";
import Header, { P } from "./Header";
import { previewDxf, uploadDxf, type DxfAlignmentInfo, type DxfChainageInfo, type DxfCrsOption, type DxfUpload } from "./api";

/** What the search needs from the two DXF files once both are ready (chainage is optional). */
export interface DxfSelection {
  alignmentToken: string;
  crs: string;
  alignMode: string;
  chainage: { token: string; crs: string; field: string } | null;
}

interface Props {
  /** Only the active input mode is shown; the state is kept while the other mode is used. */
  active: boolean;
  disabled: boolean;
  /** null while the alignment is not ready (no file, no CRS, or an error) */
  onSelection: (selection: DxfSelection | null) => void;
  onAlignmentPreview: (fc: FeatureCollection | null) => void;
  onChainagePreview: (fc: FeatureCollection | null) => void;
  onFit: (fc: FeatureCollection) => void;
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

const km = (m: number) => `${(m / 1000).toFixed(2)} km`;
const num = (n: number) => n.toLocaleString();

function CrsSelect(props: {
  options: DxfCrsOption[];
  value: string;
  other: string;
  onValue: (v: string) => void;
  onOther: (v: string) => void;
  disabled: boolean;
}) {
  return (
    <>
      <select value={props.value} onChange={(e) => props.onValue(e.target.value)} disabled={props.disabled} style={select}>
        <option value="">Select the coordinate system...</option>
        {props.options.map((o) => (
          <option key={o.code} value={String(o.code)}>
            {crsLabel(o)}
          </option>
        ))}
        <option value={OTHER}>Other EPSG code...</option>
      </select>
      {props.value === OTHER && (
        <input
          type="text"
          inputMode="numeric"
          placeholder="EPSG code, e.g. 32644"
          value={props.other}
          onChange={(e) => props.onOther(e.target.value)}
          disabled={props.disabled}
          style={{ ...select, marginTop: 4 }}
        />
      )}
    </>
  );
}

export default function DxfInputs({ active, disabled, onSelection, onAlignmentPreview, onChainagePreview, onFit }: Props) {
  // ---- alignment
  const [aUp, setAUp] = useState<DxfUpload | null>(null);
  const [aStage, setAStage] = useState<Stage>(null);
  const [aFrac, setAFrac] = useState(0);
  const [aCrs, setACrs] = useState("");
  const [aOther, setAOther] = useState("");
  const [aMode, setAMode] = useState("auto");
  const [aInfo, setAInfo] = useState<DxfAlignmentInfo | null>(null);
  const [aError, setAError] = useState<string | null>(null);
  const [aPrev, setAPrev] = useState<FeatureCollection | null>(null);
  const aSeq = useRef(0);

  // ---- chainage
  const [cUp, setCUp] = useState<DxfUpload | null>(null);
  const [cStage, setCStage] = useState<Stage>(null);
  const [cFrac, setCFrac] = useState(0);
  const [cCrs, setCCrs] = useState("");
  const [cOther, setCOther] = useState("");
  const [cField, setCField] = useState("Text");
  const [cInfo, setCInfo] = useState<DxfChainageInfo | null>(null);
  const [cError, setCError] = useState<string | null>(null);
  const [cPrev, setCPrev] = useState<FeatureCollection | null>(null);
  const cSeq = useRef(0);
  const cInput = useRef<HTMLInputElement>(null);
  // The chainage labels are in the alignment DXF itself: its upload, CRS and text columns are reused.
  const [sameFile, setSameFile] = useState(false);

  const aCrsValue = effectiveCrs(aCrs, aOther);
  const cCrsValue = sameFile ? aCrsValue : effectiveCrs(cCrs, cOther);

  async function chooseAlignment(file: File | undefined) {
    const seq = ++aSeq.current;
    setAUp(null);
    setAInfo(null);
    setAError(null);
    setAPrev(null);
    onAlignmentPreview(null);
    if (!file) return;
    setAStage("uploading");
    setAFrac(0);
    try {
      const up = await uploadDxf(file, "alignment", (f) => seq === aSeq.current && setAFrac(f), () => seq === aSeq.current && setAStage("reading"));
      if (seq !== aSeq.current) return;
      setAUp(up);
      // the CRS defined in the file, else the first one that puts the drawing on land in India
      const first = up.crs_options.find((o) => o.hint);
      setACrs(first ? String(first.code) : "");
      setAOther("");
    } catch (e) {
      if (seq === aSeq.current) setAError(message(e));
    } finally {
      if (seq === aSeq.current) setAStage(null);
    }
  }

  async function chooseChainage(file: File | undefined) {
    const seq = ++cSeq.current;
    setCUp(null);
    setCInfo(null);
    setCError(null);
    setCPrev(null);
    onChainagePreview(null);
    if (!file) return;
    setCStage("uploading");
    setCFrac(0);
    try {
      const up = await uploadDxf(file, "chainage", (f) => seq === cSeq.current && setCFrac(f), () => seq === cSeq.current && setCStage("reading"));
      if (seq !== cSeq.current) return;
      setCUp(up);
      // usually the same CRS as the alignment
      const same = aCrsValue && up.crs_options.some((o) => String(o.code) === aCrsValue) ? aCrsValue : null;
      const first = up.crs_options.find((o) => o.hint);
      setCCrs(same ?? (first ? String(first.code) : ""));
      setCOther("");
      setCField(up.default_field ?? up.fields[0]?.name ?? "Text");
    } catch (e) {
      if (seq === cSeq.current) setCError(message(e));
    } finally {
      if (seq === cSeq.current) setCStage(null);
    }
  }

  function removeChainage() {
    if (cInput.current) cInput.current.value = "";
    void chooseChainage(undefined);
  }

  function toggleSameFile(on: boolean) {
    setSameFile(on);
    if (!on) removeChainage(); // back to choosing a separate chainage file
  }

  // "Same DXF": the chainage follows the alignment upload (a separate chainage file or upload in progress is dropped).
  useEffect(() => {
    if (!sameFile) return;
    cSeq.current++;
    setCStage(null);
    setCError(null);
    if (cInput.current) cInput.current.value = "";
    setCUp(aUp);
    if (aUp) setCField(aUp.default_field ?? aUp.fields[0]?.name ?? "Text");
    else {
      setCInfo(null);
      setCPrev(null);
      onChainagePreview(null);
    }
  }, [sameFile, aUp]);

  // Draw the alignment (its longest line(s)) whenever the file, CRS or alignment type changes.
  useEffect(() => {
    if (!aUp) return;
    if (!aCrsValue) {
      setAInfo(null);
      setAPrev(null);
      onAlignmentPreview(null);
      return;
    }
    let cancelled = false;
    setAStage("drawing");
    setAError(null);
    previewDxf({ token: aUp.token, role: "alignment", crs: aCrsValue, mode: aMode })
      .then((r) => {
        if (cancelled) return;
        setAInfo(r.info as DxfAlignmentInfo);
        setAPrev(r.features);
        onAlignmentPreview(r.features);
        onFit(r.features);
      })
      .catch((e) => {
        if (cancelled) return;
        setAInfo(null);
        setAPrev(null);
        onAlignmentPreview(null);
        setAError(message(e));
      })
      .finally(() => {
        if (!cancelled) setAStage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [aUp, aCrsValue, aMode]);

  // Draw the chainage labels (text starting with CH / KM in the chosen column).
  useEffect(() => {
    if (!cUp) return;
    if (!cCrsValue || !cField) {
      setCInfo(null);
      setCPrev(null);
      onChainagePreview(null);
      return;
    }
    let cancelled = false;
    setCStage("drawing");
    setCError(null);
    previewDxf({ token: cUp.token, role: "chainage", crs: cCrsValue, field: cField })
      .then((r) => {
        if (cancelled) return;
        setCInfo(r.info as DxfChainageInfo);
        setCPrev(r.features);
        onChainagePreview(r.features);
        if (!aPrev) onFit(r.features);
      })
      .catch((e) => {
        if (cancelled) return;
        setCInfo(null);
        setCPrev(null);
        onChainagePreview(null);
        setCError(message(e));
      })
      .finally(() => {
        if (!cancelled) setCStage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [cUp, cCrsValue, cField]);

  // Back in this mode: show what was drawn before again.
  useEffect(() => {
    if (!active) return;
    onAlignmentPreview(aPrev);
    onChainagePreview(cPrev);
  }, [active]);

  // Tell the page what to search. A chainage file that is chosen but not usable blocks the search, so it is not
  // silently left out.
  useEffect(() => {
    if (!aUp || !aCrsValue || !aInfo || aStage !== null) {
      onSelection(null);
      return;
    }
    if (cUp && (!cInfo || !cCrsValue || cStage !== null)) {
      onSelection(null);
      return;
    }
    onSelection({
      alignmentToken: aUp.token,
      crs: aCrsValue,
      alignMode: aMode,
      chainage: cUp ? { token: cUp.token, crs: cCrsValue, field: cField } : null,
    });
  }, [aUp, aCrsValue, aMode, aInfo, aStage, cUp, cCrsValue, cField, cInfo, cStage]);

  const chosenField = cUp?.fields.find((f) => f.name === cField);

  return (
    <div style={{ display: active ? "block" : "none" }}>
      <span style={label}>Alignment (DXF)</span>
      <input type="file" accept=".dxf" disabled={disabled} onChange={(e) => void chooseAlignment(e.target.files?.[0])} style={{ fontSize: 12, maxWidth: "100%" }} />
      {aStage === "uploading" && <ProgressBar fraction={aFrac} label="Uploading the alignment DXF" />}
      {aStage === "reading" && <ProgressBar fraction={1} label="Reading the DXF..." indeterminate />}
      {aStage === "drawing" && <ProgressBar fraction={1} label="Drawing the alignment on the map..." indeterminate />}
      {aError && <div style={warn}>{aError}</div>}
      {aUp && (
        <>
          <span style={label}>Coordinate system of the alignment file</span>
          <CrsSelect options={aUp.crs_options} value={aCrs} other={aOther} onValue={setACrs} onOther={setAOther} disabled={disabled} />
          {!aCrsValue && <div style={small}>A DXF does not say which coordinate system it uses. The ones that put the drawing on land in India are listed first.</div>}
          <Header
            level="sub"
            title="The alignment is"
            info={
              <>
                <P>
                  Only the longest line(s) of the DXF are the alignment: every shorter line (tick marks, outlines of labels, pieces of a
                  centreline) is a marking and is ignored. <strong>Detect</strong> takes two lines when the second longest is at least half as
                  long as the longest, otherwise one. <strong>Two lines</strong> are the corridor's edges (the strip between them is searched);
                  <strong> a single centreline</strong> is searched as a line. Arcs are kept as drawn, not straightened.
                </P>
                {aInfo && (
                  <P>
                    <strong>In this file:</strong> using the {aInfo.lines_used === 2 ? "two longest lines" : "longest line"} (
                    {aInfo.lengths_m.map(km).join(" and ")}). {num(aInfo.ignored)} shorter line{aInfo.ignored === 1 ? "" : "s"} ignored as markings.
                  </P>
                )}
              </>
            }
          />
          <select value={aMode} onChange={(e) => setAMode(e.target.value)} disabled={disabled} style={select}>
            <option value="auto">Detect: the 1 or 2 longest lines</option>
            <option value="two">Two lines (the corridor's edges)</option>
            <option value="single">A single centreline</option>
          </select>
          {aInfo?.warning && <div style={warn}>{aInfo.warning}</div>}
        </>
      )}

      <div style={{ marginTop: 12 }}>
        <Header
          level="sub"
          title="Chainage (DXF, optional)"
          info={
            <>
              <P>
                The chainage labels are the text points (TEXT / MTEXT) whose text starts with <strong>CH</strong> or <strong>KM</strong>, for
                example <em>CH -2+400</em>, <em>KM: 12+500</em> or <em>CH 12500</em>. They give the parcel table its <em>chainage from / to</em> and{" "}
                <em>chainage labels inside</em> columns.
              </P>
              <P>
                Tick <strong>Chainage is in the same DXF</strong> when one file holds both the alignment and the labels: the alignment upload and
                its coordinate system are used, no second file is needed.
              </P>
              {sameFile && (
                <P>
                  <strong>Now:</strong>{" "}
                  {aUp ? `using the chainage labels (text starting with CH or KM) of ${aUp.filename}.` : "choose the alignment DXF first; its chainage labels will be used."}
                </P>
              )}
            </>
          }
        />
      </div>
      <label style={{ display: "block", fontSize: 12, marginBottom: 4 }} title="Use the CH/KM text points of the alignment DXF itself: no second file, no second upload">
        <input type="checkbox" checked={sameFile} onChange={(e) => toggleSameFile(e.target.checked)} disabled={disabled || (!aUp && !sameFile)} /> Chainage is in the
        same DXF
      </label>
      {!sameFile && (
        <>
          <input
            ref={cInput}
            type="file"
            accept=".dxf"
            disabled={disabled}
            onChange={(e) => void chooseChainage(e.target.files?.[0])}
            style={{ fontSize: 12, maxWidth: "100%" }}
          />
          {cUp && (
            <button type="button" className="mini" onClick={removeChainage} disabled={disabled} style={{ marginLeft: 6 }}>
              Remove
            </button>
          )}
        </>
      )}
      {cStage === "uploading" && <ProgressBar fraction={cFrac} label="Uploading the chainage DXF" />}
      {cStage === "reading" && <ProgressBar fraction={1} label="Reading the DXF..." indeterminate />}
      {cStage === "drawing" && <ProgressBar fraction={1} label="Reading the chainage labels..." indeterminate />}
      {cError && <div style={warn}>{cError}</div>}
      {cUp && (
        <>
          {!sameFile && (
            <>
              <span style={label}>Coordinate system of the chainage file</span>
              <CrsSelect options={cUp.crs_options} value={cCrs} other={cOther} onValue={setCCrs} onOther={setCOther} disabled={disabled} />
            </>
          )}
          <Header
            level="sub"
            title="Column with the chainage text"
            info={
              <>
                <P>
                  The text of each chainage label is read from this column. The columns of the file are listed with how many values start with CH or
                  KM and how many are filled; the one with the most CH/KM values is chosen for you. The first 10 text rows of the file are shown
                  below to help you pick.
                </P>
                {cInfo && (
                  <P>
                    <strong>In this file:</strong> {num(cInfo.count)} chainage labels read, {cInfo.first} to {cInfo.last}.
                  </P>
                )}
              </>
            }
          />
          {cUp.fields.length === 0 ? (
            <div style={warn}>This DXF has no text points (TEXT / MTEXT), so there is no chainage in it.</div>
          ) : (
            <select value={cField} onChange={(e) => setCField(e.target.value)} disabled={disabled} style={select}>
              {cUp.fields.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.name} — {f.non_empty === 0 ? "empty" : `${num(f.chainage_like)} start with CH/KM, ${num(f.non_empty)} filled`}
                </option>
              ))}
            </select>
          )}
          {chosenField && chosenField.non_empty === 0 && <div style={warn}>The column "{chosenField.name}" is empty: it holds no text. Choose another column.</div>}
          {chosenField && chosenField.non_empty > 0 && chosenField.chainage_like === 0 && (
            <div style={warn}>No value in "{chosenField.name}" starts with CH or KM. Choose another column.</div>
          )}
          {cUp.sample.rows.length > 0 && (
            <details style={{ marginTop: 4 }}>
              <summary style={{ fontSize: 11, color: "#444", cursor: "pointer" }}>First {cUp.sample.rows.length} text rows of the file</summary>
              <div style={{ overflowX: "auto", maxWidth: "100%" }}>
                <table style={{ fontSize: 10, borderCollapse: "collapse", marginTop: 2 }}>
                  <thead>
                    <tr>
                      {cUp.sample.columns.map((c) => (
                        <th key={c} style={{ textAlign: "left", padding: "1px 5px", background: c === cField ? "#cfe3ff" : "#f2f2f2", whiteSpace: "nowrap" }}>
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {cUp.sample.rows.map((row, i) => (
                      <tr key={i}>
                        {row.map((v, j) => (
                          <td key={j} style={{ padding: "1px 5px", whiteSpace: "nowrap", background: cUp.sample.columns[j] === cField ? "#eaf2ff" : undefined }}>
                            {v}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
          {cInfo && cInfo.unparsed > 0 && (
            <div style={warn}>
              {num(cInfo.unparsed)} start with CH/KM but hold no readable chainage (e.g. {cInfo.unparsed_examples.slice(0, 2).join(", ")}); they are ignored.
            </div>
          )}
        </>
      )}
    </div>
  );
}
