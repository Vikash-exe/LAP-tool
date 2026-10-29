import { useEffect, useMemo, useRef, useState } from "react";
import type { Feature, FeatureCollection } from "geojson";
import MapView, { type MapViewHandle, type ParcelLayer } from "./MapView";
import Resizer from "./Resizer";
import DxfInputs, { type DxfSelection } from "./DxfInputs";
import Header, { P, labelStyle } from "./Header";
import ProgressBar from "./ProgressBar";
import { BASEMAPS } from "./basemaps";
import {
  downloadResult,
  fetchParcels,
  fetchVillages,
  fetchChainage,
  fetchVillagesNoParcels,
  dropAllStateTables,
  dropStateTable,
  getStates,
  getMe,
  getStateTables,
  logout,
  previewArea,
  runSearch,
  type AreaStats,
  type DxfAlignmentInfo,
  type BBox,
  type ExportFormat,
  type AvailableState,
  type JobState,
  type SearchJob,
  type StateTable,
  type StateTables,
  type MatchMode,
  type QueryResult,
} from "./api";

const MIN_SIDEBAR_WIDTH = 260;
const MAX_SIDEBAR_WIDTH = 700;
const DEFAULT_PARCEL_OPACITY = 0.35;

// Distinct colours so several source layers switched on together stay
// tell-apart-able, QGIS style.
const PALETTE = ["#00acc1", "#e91e63", "#8bc34a", "#ff9800", "#7e57c2", "#009688", "#f44336", "#3f51b5"];

const MODES: { value: MatchMode; label: string; hint: string }[] = [
  { value: "intersects", label: "Intersecting", hint: "Every parcel that overlaps or touches the area (for a line: every parcel it passes through)." },
  { value: "within", label: "Fully inside", hint: "Only parcels completely contained in the area." },
  { value: "crossing", label: "Crossing the boundary", hint: "Only parcels that straddle the area's edge (partly inside, partly outside)." },
];

const FORMATS: { value: ExportFormat; label: string }[] = [
  { value: "geojson", label: "GeoJSON" },
  { value: "gpkg", label: "GeoPackage" },
  { value: "shp", label: "Shapefile (.zip)" },
  { value: "kml", label: "KML" },
  { value: "csv", label: "CSV (WKT)" },
];

// uploading: the file is going to the server; working: the search job is running (it may be loading a
// state's parcel data first); fetching: the finished result is being loaded onto the map.
type Phase = "idle" | "uploading" | "working" | "fetching";

// Value of the download selector that stands for the village boundaries layer
// (the other values are source file names).
const VILLAGES_SOURCE = "__villages__";

interface SourceGroup {
  data: FeatureCollection;
  bbox: BBox | null;
}


function fmt(n: number) {
  return n.toLocaleString("en-US");
}

// Areas arrive in square metres; hectares are the readable unit for parcels,
// with the exact m2 in the tooltip.
function fmtHa(m2: number) {
  return (m2 / 10000).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtM2(m2: number) {
  return `${Math.round(m2).toLocaleString("en-US")} m²`;
}

function fmtBytes(n: number) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  return `${Math.max(0, Math.round(n / 1e6)).toLocaleString("en-US")} MB`;
}

// "2 h 41 min", "35 min", "under a minute"
function fmtDuration(seconds: number) {
  if (seconds < 60) return "under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  return `${h} h ${minutes % 60} min`;
}

function saveText(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// The per-source area table as CSV: one row per source file plus a total.
function areaSummaryCsv(result: QueryResult): string {
  const q = (s: string) => `"${s.replace(/"/g, '""')}"`;
  const rows = [["source", "parcels", "parcel_area_m2", "inside_area_m2", "inside_pct_of_parcel_area"]];
  const add = (name: string, s: AreaStats) =>
    rows.push([q(name), String(s.count), s.area_m2.toFixed(2), s.inside_m2.toFixed(2), s.area_m2 > 0 ? ((100 * s.inside_m2) / s.area_m2).toFixed(2) : "0"]);
  Object.keys(result.stats.per_source).sort().forEach((name) => add(name, result.stats.per_source[name]));
  add("ALL SOURCES (overlapping datasets counted more than once)", result.stats.total);
  rows.push([]);
  rows.push([q(result.strip_used ? "strip_area_m2" : "search_area_m2"), (result.aoi_area_m2 ?? 0).toFixed(2)]);
  return rows.map((r) => r.join(",")).join("\n");
}

// The form fields that tell the server to search a DXF alignment (and chainage) instead of an uploaded area file.
function dxfFormFields(s: DxfSelection): Record<string, string> {
  const fields: Record<string, string> = { alignment_token: s.alignmentToken, crs: s.crs, align_mode: s.alignMode };
  if (s.chainage) {
    fields.chainage_token = s.chainage.token;
    fields.chainage_crs = s.chainage.crs;
    fields.chainage_field = s.chainage.field;
  }
  return fields;
}

function boundsOf(features: Feature[]): BBox | null {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const visit = (node: unknown): void => {
    if (!Array.isArray(node)) return;
    if (typeof node[0] === "number") {
      minX = Math.min(minX, node[0]);
      maxX = Math.max(maxX, node[0]);
      minY = Math.min(minY, node[1]);
      maxY = Math.max(maxY, node[1]);
    } else {
      node.forEach(visit);
    }
  };
  features.forEach((f) => {
    const g = f.geometry as { coordinates?: unknown } | null;
    if (g && g.coordinates) visit(g.coordinates);
  });
  return Number.isFinite(minX) ? [minX, minY, maxX, maxY] : null;
}

// One FeatureCollection per source file, so each can be its own map layer.
function groupBySource(fc: FeatureCollection): Record<string, SourceGroup> {
  const buckets: Record<string, Feature[]> = {};
  for (const f of fc.features) {
    const source = String(f.properties?.source ?? "unknown");
    (buckets[source] ??= []).push(f);
  }
  return Object.fromEntries(
    Object.entries(buckets).map(([source, features]) => [
      source,
      { data: { type: "FeatureCollection", features } as FeatureCollection, bbox: boundsOf(features) },
    ])
  );
}

export default function App() {
  const mapViewRef = useRef<MapViewHandle>(null);
  const [sidebarWidth, setSidebarWidth] = useState(400);

  // Parcel data is loaded into the database per state, only when a search needs it, and dropped
  // again after a while. `availableStates` is what exists on the mount; `stateTables` is what is in
  // the database right now.
  const [availableStates, setAvailableStates] = useState<AvailableState[]>([]);
  const [stateTables, setStateTables] = useState<StateTables | null>(null);
  const [tablesFetchedAt, setTablesFetchedAt] = useState(Date.now());
  const [, setClockTick] = useState(0); // re-renders the "drops in ..." countdowns
  const [maxFeatures, setMaxFeatures] = useState<number | null>(null);
  const [ttlSeconds, setTtlSeconds] = useState<number | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [me, setMe] = useState<{ user: string | null; auth: boolean } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [job, setJob] = useState<SearchJob | null>(null);

  const [files, setFiles] = useState<File[]>([]);
  // The chosen file as read by the server, shown on the map (and zoomed to) before any search is run.
  const [preview, setPreview] = useState<FeatureCollection | null>(null);
  const previewSeq = useRef(0); // ignores the answer for a file that was replaced meanwhile
  // Loading bar while the chosen file is sent to the server and converted: uploading, then reading.
  const [previewStage, setPreviewStage] = useState<"uploading" | "reading" | null>(null);
  // For an area file made of lines: which lines were found and used (shown in the info card of section 1).
  const [areaAlignment, setAreaAlignment] = useState<DxfAlignmentInfo | null>(null);
  const [previewFraction, setPreviewFraction] = useState(0);
  const [mode, setMode] = useState<MatchMode>("intersects");
  // When the upload is exactly two lines, search the strip between them.
  const [stripMode, setStripMode] = useState(true);

  const [phase, setPhase] = useState<Phase>("idle");
  const [uploadFraction, setUploadFraction] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [parcels, setParcels] = useState<FeatureCollection | null>(null);
  const [clip, setClip] = useState<FeatureCollection | null>(null);
  const [villages, setVillages] = useState<FeatureCollection | null>(null);
  const [download, setDownload] = useState<{ label: string; fraction: number | null } | null>(null);
  const [downloadSource, setDownloadSource] = useState("");
  const [splitGpkg, setSplitGpkg] = useState(false);
  const [downloadClipped, setDownloadClipped] = useState(false);

  const [basemapId, setBasemapId] = useState<string | null>("google-roadmap");
  const [showAoi, setShowAoi] = useState(true);
  const [aoiOpacity, setAoiOpacity] = useState(1);
  const [showStrip, setShowStrip] = useState(true);
  const [stripOpacity, setStripOpacity] = useState(1);
  const [showClip, setShowClip] = useState(true);
  const [clipOpacity, setClipOpacity] = useState(0.5);
  // Colours the user picked. Parcel layers are keyed by source file name and the
  // choice is kept across searches; without one a layer uses its palette colour.
  const [aoiColor, setAoiColor] = useState("#ff1744");
  const [stripColor, setStripColor] = useState("#ffb300");
  const [clipColor, setClipColor] = useState("#d500f9");
  const [showVillages, setShowVillages] = useState(true);
  const [villageOpacity, setVillageOpacity] = useState(1);
  const [villageColor, setVillageColor] = useState("#2e7d32");
  // Villages found that have no parcels in the database: their own layer, in a different colour.
  const [villagesNoParcels, setVillagesNoParcels] = useState<FeatureCollection | null>(null);
  const [showNoParcels, setShowNoParcels] = useState(true);
  const [noParcelsOpacity, setNoParcelsOpacity] = useState(1);
  const [noParcelsColor, setNoParcelsColor] = useState("#ff6d00");
  // Input: an area file, or a DXF alignment (+ chainage). The chainage labels are drawn as a layer of their own.
  const [inputKind, setInputKind] = useState<"file" | "dxf">("file");
  const [dxfSelection, setDxfSelection] = useState<DxfSelection | null>(null);
  const [chainagePreview, setChainagePreview] = useState<FeatureCollection | null>(null);
  const [chainageLayer, setChainageLayer] = useState<FeatureCollection | null>(null);
  const [showChainage, setShowChainage] = useState(true);
  const [chainageOpacity, setChainageOpacity] = useState(1);
  const [chainageColor, setChainageColor] = useState("#1565c0");
  const [layerColor, setLayerColor] = useState<Record<string, string>>({});
  // Per source-file layer state, keyed by source file name. layerOrder runs
  // bottom (first) to top (last) of the map stack.
  const [layerOrder, setLayerOrder] = useState<string[]>([]);
  const [layerVisible, setLayerVisible] = useState<Record<string, boolean>>({});
  const [layerOpacity, setLayerOpacity] = useState<Record<string, number>>({});

  async function refreshStateTables() {
    try {
      setStateTables(await getStateTables());
      setTablesFetchedAt(Date.now());
    } catch (e) {
      setBackendError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    getMe().then(setMe).catch(() => {}); // who is signed in
    getStates()
      .then(({ states, max_features, ttl_seconds }) => {
        setAvailableStates(states);
        setMaxFeatures(max_features);
        setTtlSeconds(ttl_seconds);
      })
      .catch((e) => setBackendError(e instanceof Error ? e.message : String(e)));
    refreshStateTables();
  }, []);

  // Keeps the loaded-states panel current (tables expire on their own), and its countdowns ticking.
  useEffect(() => {
    const poll = window.setInterval(refreshStateTables, 10000);
    const tick = window.setInterval(() => setClockTick((n) => n + 1), 15000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, []);

  // A new result starts with all of its source layers on, in a fixed order.
  useEffect(() => {
    const names = Object.keys(result?.per_source ?? {}).sort();
    setLayerOrder(names);
    setLayerVisible(Object.fromEntries(names.map((n) => [n, true])));
    setLayerOpacity({});
    // With no parcels but some villages, the village layer is the only thing to download.
    setDownloadSource(result && result.count === 0 && (result.villages?.count ?? 0) > 0 ? VILLAGES_SOURCE : "");
    setSplitGpkg(false);
    setDownloadClipped(false);
  }, [result?.result_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const groups = useMemo(() => (parcels ? groupBySource(parcels) : {}), [parcels]);
  const sourceNames = useMemo(() => Object.keys(result?.per_source ?? {}).sort(), [result?.result_id]); // eslint-disable-line react-hooks/exhaustive-deps
  const colorOf = (name: string) => layerColor[name] ?? PALETTE[Math.max(0, sourceNames.indexOf(name)) % PALETTE.length];
  const opacityOf = (name: string) => layerOpacity[name] ?? DEFAULT_PARCEL_OPACITY;

  const parcelLayers: ParcelLayer[] = layerOrder
    .filter((name) => groups[name])
    .map((name) => ({
      id: `s${sourceNames.indexOf(name)}`,
      color: colorOf(name),
      data: groups[name].data,
      visible: layerVisible[name] ?? true,
      opacity: opacityOf(name),
    }));

  function moveLayer(name: string, direction: -1 | 1) {
    setLayerOrder((prev) => {
      const i = prev.indexOf(name);
      const j = i + direction;
      if (i < 0 || j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }

  function resizeSidebar(deltaX: number) {
    setSidebarWidth((w) => Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, w + deltaX)));
  }

  // Drops one state's whole parcel table (after a confirmation).
  async function handleDropState(state: string) {
    if (!confirm(`Drop the parcel table for ${state} from the database?\n\nIt is loaded again automatically the next time a search needs it.`)) return;
    setNotice(null);
    try {
      const { freed_bytes } = await dropStateTable(state);
      setNotice(`${state} dropped (${fmtBytes(freed_bytes)} freed).`);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    }
    await refreshStateTables();
  }

  async function handleDropAll() {
    if (!confirm("Drop every loaded parcel table from the database?\n\nThey are loaded again automatically when a search needs them.")) return;
    setNotice(null);
    try {
      const r = await dropAllStateTables();
      const skipped = r.skipped.length ? ` Not dropped (in use): ${r.skipped.map((s) => s.state).join(", ")}.` : "";
      setNotice(`${r.dropped.length} state table(s) dropped (${fmtBytes(r.freed_bytes)} freed).${skipped}`);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    }
    await refreshStateTables();
  }

  async function handleFilesChosen(chosen: File[]) {
    const seq = ++previewSeq.current;
    setFiles(chosen);
    setError(null);
    setPreview(null);
    setAreaAlignment(null);
    if (chosen.length === 0) {
      setPreviewStage(null);
      return;
    }
    setPreviewStage("uploading");
    setPreviewFraction(0);
    try {
      const { aoi: fc, alignment } = await previewArea(chosen, (f) => seq === previewSeq.current && setPreviewFraction(f), () => seq === previewSeq.current && setPreviewStage("reading"));
      if (seq !== previewSeq.current) return;
      setPreview(fc);
      setAreaAlignment(alignment);
      setShowAoi(true);
      const b = boundsOf(fc.features);
      if (b) mapViewRef.current?.fitBounds(b);
    } catch (e) {
      if (seq !== previewSeq.current) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === previewSeq.current) setPreviewStage(null);
    }
  }

  async function signOut() {
    await logout();
    window.location.href = "/login";
  }

  function switchInputKind(kind: "file" | "dxf") {
    if (kind === inputKind) return;
    previewSeq.current++;
    setPreviewStage(null);
    setError(null);
    setPreview(null);
    setAreaAlignment(null);
    setChainagePreview(null);
    setInputKind(kind);
    if (kind === "file" && files.length > 0) void handleFilesChosen(files); // draw the chosen area file again
  }

  function fitTo(fc: FeatureCollection) {
    const b = boundsOf(fc.features);
    if (b) mapViewRef.current?.fitBounds(b);
  }

  async function handleSearch() {
    if (inputKind === "file" ? files.length === 0 : !dxfSelection) return;
    setError(null);
    setNotice(null);
    setResult(null);
    setParcels(null);
    setClip(null);
    setVillages(null);
    setVillagesNoParcels(null);
    setChainageLayer(null);
    setJob(null);
    setPhase("uploading");
    setUploadFraction(0);
    try {
      // The server finds the state(s) the area falls in, loads their parcel data if it is not in the
      // database yet (the job reports progress), and then searches.
      const res = await runSearch(inputKind === "dxf" ? [] : files, mode, [], stripMode, {
        onUploadProgress: setUploadFraction,
        onUploadComplete: () => setPhase("working"),
        onJob: (j) => {
          setJob(j);
          setPhase("working");
        },
      }, inputKind === "dxf" && dxfSelection ? dxfFormFields(dxfSelection) : undefined);
      setResult(res);
      setPreview(null); // the result carries the area now
      setChainagePreview(null);
      refreshStateTables();
      const villageCount = res.villages?.count ?? 0;
      if (res.count > 0 || villageCount > 0) {
        setPhase("fetching");
        const [full, clipped, villageFc, noParcelFc] = await Promise.all([
          res.count > 0 ? fetchParcels(res.result_id) : Promise.resolve(null),
          res.count > 0 && res.aoi_area_m2 ? fetchParcels(res.result_id, true) : Promise.resolve(null),
          villageCount > 0 ? fetchVillages(res.result_id) : Promise.resolve(null),
          (res.villages?.no_parcels?.count ?? 0) > 0 ? fetchVillagesNoParcels(res.result_id) : Promise.resolve(null),
        ]);
        setParcels(full);
        setClip(clipped);
        setVillages(villageFc);
        setVillagesNoParcels(noParcelFc);
      }
      if ((res.chainage?.labels_on_alignment ?? 0) > 0) setChainageLayer(await fetchChainage(res.result_id));
      // Frame whatever matched: the parcels, else the villages, else the searched area.
      const target = res.count > 0 ? res.bbox : villageCount > 0 ? res.villages?.bbox : null;
      const b = target ?? boundsOf(res.aoi.features);
      if (b) mapViewRef.current?.fitBounds(b);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      refreshStateTables();
    } finally {
      setPhase("idle");
      setJob(null);
    }
  }

  async function handleDownload(format: ExportFormat, label: string) {
    if (!result) return;
    const villagesLayer = downloadSource === VILLAGES_SOURCE;
    const split = splitGpkg && format === "gpkg" && !downloadSource;
    const clipped = downloadClipped && !!result.aoi_area_m2;
    const what = villagesLayer ? "village boundaries" : split ? "one layer per source" : downloadSource || "all sources";
    setDownload({ label: `Preparing ${label} (${what}${clipped ? ", clipped" : ""})...`, fraction: null });
    try {
      await downloadResult(
        result.result_id,
        format,
        { source: villagesLayer ? null : downloadSource || null, split, clipped, villages: villagesLayer },
        (fraction) =>
        setDownload({ label: `Downloading ${label}`, fraction })
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDownload(null);
    }
  }

  // The CSV of the villages found that have no cadastral parcels in the database.
  async function handleDownloadNoParcels() {
    if (!result) return;
    setDownload({ label: "Preparing the villages without parcels (CSV)...", fraction: null });
    try {
      await downloadResult(result.result_id, "csv", { villagesNoParcels: true }, (fraction) =>
        setDownload({ label: "Downloading the villages without parcels (CSV)", fraction })
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDownload(null);
    }
  }

  function handleClear() {
    previewSeq.current++;
    setPreview(null);
    setPreviewStage(null);
    setResult(null);
    setParcels(null);
    setClip(null);
    setVillages(null);
    setVillagesNoParcels(null);
    setChainageLayer(null);
    setError(null);
    setFiles([]);
  }

  const busy = phase !== "idle";
  const multiSource = sourceNames.length > 1;
  const villageCount = result?.villages?.count ?? 0;
  const villagesSelected = downloadSource === VILLAGES_SOURCE;

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <img className="logo" src="/favicon.svg" alt="" />
          <div className="brand-text">
            <Header
              className="page-title"
              level="page"
              title="Cadastral Parcel Finder"
              info={
                <>
                  <P>Upload an area (KML, Shapefile, GeoJSON...) and get every cadastral parcel that intersects it, together with the village boundaries it lies in.</P>
                  <P>Work down the panel: 1. choose the area, 2. choose how to match, then press Find parcels. The result, the layers and the downloads appear below.</P>
                </>
              }
            />
            <div className="tagline">Find the parcels and villages along an area or an alignment</div>
          </div>
        </div>
        {me?.user && (
          <div className="user-chip">
            <span>
              Signed in as <strong>{me.user}</strong>
            </span>
            <button className="mini on-dark" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        )}
      </header>

      <div className="app-body">
      <aside className="sidebar" style={{ width: sidebarWidth }}>

        <section className="card c-data">
          <Header
            className="card-head"
            title="Parcel data in the database"
            info={
              <>
                <P>
                  A state's parcels are loaded automatically when your area falls in it, and dropped again{" "}
                  <strong>{ttlSeconds ? fmtDuration(ttlSeconds) : "a few hours"}</strong> after they were last used.
                </P>
                <P><strong>Drop</strong> removes one state's whole table at once; <strong>Clear all</strong> removes every table that is not being used. A dropped state is loaded again, automatically, the next time a search needs it.</P>
                <P>The budget is the most the loaded tables may take together. A state that would not fit is refused, with a message, until you drop another one.</P>
                <P>In the list of states, the size shown is what each takes in the database once loaded.</P>
              </>
            }
          />
          {backendError && <div style={{ color: "#b00020", fontSize: 12 }}>Backend error: {backendError}</div>}
          {stateTables && stateTables.tables.length === 0 && !backendError && (
            <div style={{ fontSize: 12, color: "#999" }}>Nothing loaded right now.</div>
          )}
          {stateTables?.tables.map((t) => (
            <StateTableRow
              key={t.state}
              table={t}
              secondsLeft={t.expires_in_seconds === null ? null : t.expires_in_seconds - (Date.now() - tablesFetchedAt) / 1000}
              onDrop={() => handleDropState(t.state)}
              disabled={t.status === "loading"}
            />
          ))}
          {stateTables && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}>
              <span style={{ fontSize: 11, color: "#888", flex: 1 }}>
                {fmtBytes(stateTables.in_use_bytes)} of {fmtBytes(stateTables.budget_bytes)} budget in use
              </span>
              {stateTables.tables.length > 0 && (
                <button onClick={handleDropAll} className="mini" title="Drop every loaded state table">
                  Clear all
                </button>
              )}
            </div>
          )}
          {notice && <div style={{ marginTop: 6, fontSize: 12, color: "#444", background: "#f3f6f9", padding: 6, borderRadius: 4 }}>{notice}</div>}
          {availableStates.length > 0 && (
            <details style={{ marginTop: 6 }}>
              <summary style={{ fontSize: 12, cursor: "pointer", color: "#666" }}>
                {availableStates.length} states have parcel data to load
              </summary>
              {availableStates.map((s) => (
                <div key={s.state} style={{ display: "flex", fontSize: 11, color: "#555", padding: "1px 0 1px 8px" }}>
                  <span style={{ flex: 1 }}>{s.state}</span>
                  <span title={`${fmtBytes(s.parquet_bytes)} of parquet in ${s.files} file(s)`}>~{fmtBytes(s.est_bytes)}</span>
                  <span style={{ width: 70, textAlign: "right", color: s.status === "ready" ? "#2e7d32" : "#999" }}>
                    {s.status === "ready" ? "loaded" : s.status === "loading" ? "loading" : ""}
                  </span>
                </div>
              ))}
            </details>
          )}
        </section>

        <section className="card c-area">
          <Header
            className="card-head"
            title="1. Area of interest"
            info={
              <>
                <P>
                  Upload a <strong>.kml</strong>, <strong>.kmz</strong>, <strong>.geojson</strong> or <strong>.gpkg</strong> file, a zipped
                  shapefile (<strong>.zip</strong>), or select the shapefile parts together (.shp + .dbf + .shx + .prj). Polygons, lines
                  and points all work.
                </P>
                <P>
                  <strong>Lines are an alignment, found automatically.</strong> When the file holds lines (and no polygon), elevation is
                  ignored, line pieces whose ends meet are joined (small gaps and overlaps are closed, longer breaks are bridged where the
                  other edge shows the alignment goes on), and the two lines that run side by side, 4 to 400 m apart, are the edges of the
                  alignment. A line between the edges, ticks, side roads and lines that do not run alongside another are ignored. If no two
                  lines run side by side, the longest line, or the two longest, are used. Polygons are searched as drawn.
                </P>
                <P>
                  <strong>Two lines = the strip between them.</strong> With two lines, the area between them is searched and each
                  parcel's area inside the strip is measured. Untick the box to treat them as two separate lines instead.
                </P>
                {areaAlignment && (
                  <P>
                    <strong>In this file:</strong> using the{" "}
                    {areaAlignment.method === "parallel_pair" ? "two parallel lines" : areaAlignment.lines_used === 2 ? "two longest lines" : "longest line"} (
                    {areaAlignment.lengths_m.map((m) => `${(m / 1000).toFixed(2)} km`).join(" and ")}).{" "}
                    {areaAlignment.width_m
                      ? areaAlignment.width_m[2] - areaAlignment.width_m[0] < 2
                        ? `They run about ${Math.round(areaAlignment.width_m[1])} m apart. `
                        : `They run ${Math.round(areaAlignment.width_m[0])} to ${Math.round(areaAlignment.width_m[2])} m apart (typically ${Math.round(areaAlignment.width_m[1])} m). `
                      : ""}
                    {(areaAlignment.pieces ?? 0) > areaAlignment.lines_found
                      ? `${areaAlignment.pieces!.toLocaleString()} pieces were joined into ${areaAlignment.lines_found.toLocaleString()} lines. `
                      : ""}
                    {areaAlignment.bridged && areaAlignment.bridged.count > 0
                      ? `${areaAlignment.bridged.count} gap${areaAlignment.bridged.count === 1 ? "" : "s"} (${Math.round(areaAlignment.bridged.metres).toLocaleString()} m in all) bridged. `
                      : ""}
                    {areaAlignment.ignored.toLocaleString()} other line{areaAlignment.ignored === 1 ? "" : "s"} ignored.
                  </P>
                )}
              </>
            }
          />
          <div style={{ fontSize: 12, marginBottom: 6 }}>
            <label style={{ marginRight: 12 }}>
              <input type="radio" name="inputkind" checked={inputKind === "file"} onChange={() => switchInputKind("file")} disabled={busy} /> Area file
            </label>
            <label title="An alignment drawn in a DXF file, optionally with chainage labels from a second DXF">
              <input type="radio" name="inputkind" checked={inputKind === "dxf"} onChange={() => switchInputKind("dxf")} disabled={busy} /> DXF alignment + chainage
            </label>
          </div>
          <DxfInputs
            active={inputKind === "dxf"}
            disabled={busy}
            onSelection={setDxfSelection}
            onAlignmentPreview={setPreview}
            onChainagePreview={setChainagePreview}
            onFit={fitTo}
          />
          {inputKind === "file" && (
            <>
          <input
            type="file"
            multiple
            accept=".kml,.kmz,.zip,.geojson,.json,.gpkg,.gml,.shp,.dbf,.shx,.prj,.cpg"
            onChange={(e) => handleFilesChosen(Array.from(e.target.files ?? []))}
            disabled={busy}
            style={{ fontSize: 12, maxWidth: "100%" }}
          />
          {previewStage === "uploading" && <ProgressBar fraction={previewFraction} label="Uploading the file" />}
          {previewStage === "reading" && <ProgressBar fraction={1} label="Reading the file and drawing it on the map..." indeterminate />}
          {areaAlignment?.warning && <div style={{ fontSize: 11, color: "#b00020", marginTop: 3 }}>{areaAlignment.warning}</div>}
            </>
          )}
          <label style={{ display: "block", fontSize: 12, marginTop: 6 }} title="Applies only when the file holds exactly two lines">
            <input type="checkbox" checked={stripMode} onChange={(e) => setStripMode(e.target.checked)} disabled={busy} /> Two lines = the
            strip between them
          </label>
        </section>

        <section className="card c-match">
          <Header
            className="card-head"
            title="2. Match"
            info={
              <>
                <P>Which parcels count as matching the area:</P>
                {MODES.map((m) => (
                  <P key={m.value}>
                    <strong>{m.label}</strong>: {m.hint}
                  </P>
                ))}
                <P>The same rule is applied to the village boundaries.</P>
              </>
            }
          />
          {MODES.map((m) => (
            <label key={m.value} style={{ display: "block", fontSize: 13, padding: "2px 0" }} title={m.hint}>
              <input type="radio" name="mode" checked={mode === m.value} onChange={() => setMode(m.value)} disabled={busy} /> {m.label}
            </label>
          ))}

          <button
            onClick={handleSearch}
            disabled={(inputKind === "file" ? files.length === 0 : !dxfSelection) || busy || !!backendError}
            className="primary big" style={{ marginTop: 12 }}
          >
            Find parcels
          </button>
          {result && (
            <button onClick={handleClear} disabled={busy} style={{ marginTop: 12, marginLeft: 8 }}>
              Clear
            </button>
          )}
          {phase === "uploading" && <ProgressBar fraction={uploadFraction} label="Uploading" />}
          {phase === "working" && <JobProgress job={job} />}
          {phase === "fetching" && <ProgressBar fraction={1} label={`Loading ${fmt(result?.count ?? 0)} parcels onto the map...`} indeterminate />}
          {error && (
            <div style={{ marginTop: 8, padding: 8, background: "#fdecea", color: "#b00020", fontSize: 12, borderRadius: 4 }}>{error}</div>
          )}
        </section>

        {result && (
          <section className="card c-result">
            <Header
              className="card-head"
              title="Result"
              info={
                <>
                  <P>
                    <strong>Area falls in</strong> lists the states the area touches, found from the state boundaries. "Loaded for this
                    search" means that state's parcels were not in the database yet and were loaded for you; "no parcel data" means that
                    state has no parcel files (its village boundaries are still shown).
                  </P>
                  <P>
                    <strong>Village boundaries</strong> are a separate layer under the parcels. <em>Village ha</em> is the whole area of
                    those villages; <em>Inside ha</em> is the part of them within your area.
                  </P>
                  {maxFeatures !== null && (
                    <P>
                      One search returns at most {fmt(maxFeatures)} parcels. If more match, use a smaller area, restrict to fewer states, or
                      choose "Fully inside".
                    </P>
                  )}
                </>
              }
            />
            <div style={{ fontSize: 22, fontWeight: "bold" }}>
              {fmt(result.count)} <span style={{ fontSize: 13, fontWeight: "normal" }}>parcels</span>
            </div>
            <div style={{ fontSize: 11, color: "#888", marginBottom: 6 }}>
              found in {result.seconds}s{multiSource ? ` · ${sourceNames.length} source layers` : ""}
            </div>
            <div style={{ fontSize: 12, margin: "2px 0 6px" }}>
              <span style={{ color: "#666" }}>Area falls in: </span>
              {result.detected_states.map((s, i) => (
                <span key={s.name}>
                  {i > 0 && ", "}
                  <strong>{s.name}</strong>
                  {s.status === "no_data" && (
                    <span style={{ color: "#b26a00" }} title={s.note}>
                      {" "}(no parcel data)
                    </span>
                  )}
                  {s.loaded_now && <span style={{ color: "#2e7d32" }}> (loaded for this search)</span>}
                </span>
              ))}
            </div>
            {result.alignment && (
              <div style={{ fontSize: 12, margin: "2px 0 6px" }}>
                <span style={{ color: "#666" }}>Alignment: </span>
                {result.alignment.method === "parallel_pair" ? "two parallel lines" : result.alignment.lines_used === 2 ? "two longest lines" : "longest line"} (
                {result.alignment.lengths_m.map((m) => `${(m / 1000).toFixed(2)} km`).join(", ")}); {fmt(result.alignment.ignored)} other lines ignored
                {(result.alignment.pieces ?? 0) > result.alignment.lines_found ? `; ${fmt(result.alignment.pieces!)} pieces joined` : ""}
              </div>
            )}
            {result.chainage && !result.chainage.error && (
              <div style={{ fontSize: 12, margin: "2px 0 6px" }}>
                <span style={{ color: "#666" }}>Chainage: </span>
                <span style={{ color: chainageColor, fontWeight: "bold" }}>
                  {result.chainage.first} to {result.chainage.last}
                </span>{" "}
                ({fmt(result.chainage.labels_on_alignment ?? 0)} labels on the alignment
                {(result.chainage.labels_far ?? 0) > 0 ? `, ${fmt(result.chainage.labels_far ?? 0)} elsewhere in the file ignored` : ""}); the parcel table gets
                chainage from/to and the labels inside each parcel
              </div>
            )}
            {result.chainage?.error && (
              <div style={{ fontSize: 12, margin: "2px 0 6px", color: "#b00020" }}>Chainage could not be applied: {result.chainage.error}</div>
            )}
            {result.count === 0 && (
              <div style={{ fontSize: 12, color: "#666" }}>
                {result.detected_states.every((s) => s.status === "no_data")
                  ? `${result.detected_states[0]?.note ? result.detected_states[0].note[0].toUpperCase() + result.detected_states[0].note.slice(1) : "There is no parcel data for the state(s) this area falls in"}; village boundaries are shown where they exist.`
                  : "No parcels match this area."}
              </div>
            )}
            {Object.entries(result.per_state).map(([state, n]) => (
              <div key={state} style={{ fontSize: 12 }}>
                <strong>{state}</strong>: {fmt(n)}
              </div>
            ))}

            {result.villages && (
              <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid #eee" }}>
                {result.villages.error ? (
                  <div style={{ fontSize: 12, color: "#b00020" }}>Village boundaries: {result.villages.error}</div>
                ) : (
                  <>
                    <div style={{ fontSize: 16, fontWeight: "bold", color: villageColor }}>
                      {fmt(villageCount)} <span style={{ fontSize: 12, fontWeight: "normal", color: "#222" }}>village boundaries</span>
                    </div>
                    {villageCount === 0 && <div style={{ fontSize: 12, color: "#666" }}>No village boundary lies in this area.</div>}
                    {(result.villages.no_parcels?.count ?? 0) > 0 && (
                      <div style={{ fontSize: 12, fontWeight: "bold", color: noParcelsColor, marginTop: 2 }}>
                        {fmt(result.villages.no_parcels!.count)} of them {result.villages.no_parcels!.count === 1 ? "has" : "have"} no parcels in the
                        database (shown on the map in this colour)
                      </div>
                    )}
                    {result.villages.no_parcels?.error && (
                      <div style={{ fontSize: 12, color: "#b00020", marginTop: 2 }}>Villages without parcels: {result.villages.no_parcels.error}</div>
                    )}
                    {villageCount > 0 && result.aoi_area_m2 && result.villages.stats && (
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, marginTop: 4 }}>
                        <thead>
                          <tr style={{ color: "#666", textAlign: "right", borderBottom: "1px solid #ddd" }}>
                            <th style={{ textAlign: "left", fontWeight: "normal" }}>State</th>
                            <th style={{ fontWeight: "normal" }}>Villages</th>
                            <th style={{ fontWeight: "normal" }} title="Whole area of these villages">Village ha</th>
                            <th style={{ fontWeight: "normal" }} title="Part of them inside the area">Inside ha</th>
                          </tr>
                        </thead>
                        <tbody>
                          {Object.entries(result.villages.stats.per_state).map(([state, s]) => (
                            <tr key={state} style={{ textAlign: "right" }}>
                              <td style={{ textAlign: "left" }}>{state}</td>
                              <td>{fmt(s.count)}</td>
                              <td title={fmtM2(s.area_m2)}>{fmtHa(s.area_m2)}</td>
                              <td title={fmtM2(s.inside_m2)}>{fmtHa(s.inside_m2)}</td>
                            </tr>
                          ))}
                          {Object.keys(result.villages.stats.per_state).length > 1 && (
                            <tr style={{ textAlign: "right", fontWeight: "bold", borderTop: "1px solid #ddd" }}>
                              <td style={{ textAlign: "left" }}>All</td>
                              <td>{fmt(result.villages.stats.total.count)}</td>
                              <td title={fmtM2(result.villages.stats.total.area_m2)}>{fmtHa(result.villages.stats.total.area_m2)}</td>
                              <td title={fmtM2(result.villages.stats.total.inside_m2)}>{fmtHa(result.villages.stats.total.inside_m2)}</td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    )}
                  </>
                )}
              </div>
            )}
            {result.count > 0 && result.aoi_area_m2 && (
              <div style={{ marginTop: 10 }}>
                <Header
                  level="sub"
                  title={
                    <>
                      Areas ({result.strip_used ? "strip" : "search area"}: <span title={fmtM2(result.aoi_area_m2)}>{fmtHa(result.aoi_area_m2)} ha</span>)
                    </>
                  }
                  info={
                    <>
                      <P>
                        <strong>Parcel ha</strong>: the whole parcels that touch the area. <strong>Inside ha</strong>: only the part of
                        them that lies within it. Areas are measured on the ellipsoid; each parcel also carries <code>area_m2</code>,{" "}
                        <code>inside_m2</code> and <code>inside_pct</code> in its attributes.
                      </P>
                      <P>Hover a figure to see it in square metres.</P>
                      {multiSource && (
                        <P>
                          <strong>All*</strong>: different source files map the same land, so this row counts it once per source. Use one
                          source's row for the true area.
                        </P>
                      )}
                      <P>"Download area summary" saves this table as a CSV.</P>
                    </>
                  }
                />
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ color: "#666", textAlign: "right", borderBottom: "1px solid #ddd" }}>
                      <th style={{ textAlign: "left", fontWeight: "normal" }}>Source</th>
                      <th style={{ fontWeight: "normal" }} title="Number of parcels">Parcels</th>
                      <th style={{ fontWeight: "normal" }} title="Total area of these parcels, whole">Parcel ha</th>
                      <th style={{ fontWeight: "normal" }} title="Part of those parcels lying inside the area">Inside ha</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sourceNames.map((name) => {
                      const s = result.stats.per_source[name];
                      return (
                        <tr key={name} style={{ textAlign: "right" }}>
                          <td style={{ textAlign: "left", maxWidth: 110, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={name}>
                            {name.replace(/_?Cadastrals?\.parquet$/i, "").replace(/\.parquet$/i, "")}
                          </td>
                          <td>{fmt(s.count)}</td>
                          <td title={fmtM2(s.area_m2)}>{fmtHa(s.area_m2)}</td>
                          <td title={`${fmtM2(s.inside_m2)} (${s.area_m2 > 0 ? ((100 * s.inside_m2) / s.area_m2).toFixed(1) : "0"}% of these parcels)`}>
                            {fmtHa(s.inside_m2)}
                          </td>
                        </tr>
                      );
                    })}
                    {multiSource && (
                      <tr style={{ textAlign: "right", fontWeight: "bold", borderTop: "1px solid #ddd" }}>
                        <td style={{ textAlign: "left" }}>All*</td>
                        <td>{fmt(result.stats.total.count)}</td>
                        <td title={fmtM2(result.stats.total.area_m2)}>{fmtHa(result.stats.total.area_m2)}</td>
                        <td title={fmtM2(result.stats.total.inside_m2)}>{fmtHa(result.stats.total.inside_m2)}</td>
                      </tr>
                    )}
                  </tbody>
                </table>
                <button
                  onClick={() => saveText(areaSummaryCsv(result), `area_summary_${result.result_id.slice(0, 8)}.csv`, "text/csv")}
                  style={{ fontSize: 12, marginTop: 4 }}
                >
                  Download area summary (CSV)
                </button>
              </div>
            )}

            {(result.count > 0 || villageCount > 0) && (
              <div className="inner" style={{ marginTop: 12 }}>
                <Header
                  level="sub"
                  title="Download"
                  info={
                    <>
                      <P>
                        Choose what to export in the list (all parcel layers together, one source file, or the village boundaries), then a
                        format. Every original attribute column is included. Shapefile column names are cut to 10 characters by the format
                        itself; GeoPackage and GeoJSON keep full names.
                      </P>
                      {result.aoi_area_m2 && (
                        <P>
                          <strong>Whole parcels / Only the part inside</strong>: export each parcel as it is, or cut to the part that lies
                          inside the {result.strip_used ? "strip" : "area"}.
                        </P>
                      )}
                      {multiSource && (
                        <P>
                          <strong>GeoPackage: one layer per source file</strong> puts every source file in a single GeoPackage, one layer each.
                        </P>
                      )}
                      {result.aoi_area_m2 && result.count > 0 && (
                        <P>
                          <strong>Parcel tables</strong>: one CSV per source file with the columns <em>total area</em> (whole parcel),{" "}
                          <em>area affected</em> (the part inside the {result.strip_used ? "strip" : "area"}), <em>parcel number</em>,{" "}
                          <em>village name</em>, <em>taluka</em> and <em>district</em>. Areas are in square metres; parcels that only touch the
                          edge are left out. With all layers selected you get a .zip with a CSV for every source file (header only if it has
                          nothing inside the area); pick one source for just its CSV.
                        </P>
                      )}
                      {(result.chainage?.labels_on_alignment ?? 0) > 0 && (
                        <P>
                          <strong>Chainage</strong>: the parcel tables get <em>chainage from</em> and <em>chainage to</em> (where the part of the
                          parcel in the {result.strip_used ? "strip" : "searched line"} begins and ends along the alignment, interpolated between
                          the two chainage labels around it) and <em>chainage labels inside</em> (the labels that fall inside the parcel).
                          {!result.aoi_area_m2 && " A search along a single line lists every parcel the line crosses, so area affected is empty."}
                        </P>
                      )}
                    </>
                  }
                />
                {result.aoi_area_m2 && (
                  <div style={{ fontSize: 12, marginBottom: 6 }}>
                    <label style={{ marginRight: 12 }} title="Each parcel as it is, with its area columns">
                      <input type="radio" name="geometry" checked={!downloadClipped} onChange={() => setDownloadClipped(false)} /> Whole parcels
                    </label>
                    <label title="Each parcel cut to the part inside the area">
                      <input type="radio" name="geometry" checked={downloadClipped} onChange={() => setDownloadClipped(true)} /> Only the part inside
                    </label>
                  </div>
                )}
                {(multiSource || villageCount > 0) && (
                  <select
                    value={downloadSource}
                    onChange={(e) => setDownloadSource(e.target.value)}
                    disabled={download !== null}
                    style={{ width: "100%", marginBottom: 6, fontSize: 12 }}
                    title="Which layer to export"
                  >
                    {result.count > 0 && <option value="">All parcel source layers together ({fmt(result.count)})</option>}
                    {sourceNames.map((name) => (
                      <option key={name} value={name}>
                        {name} ({fmt(result.per_source[name])})
                      </option>
                    ))}
                    {villageCount > 0 && <option value={VILLAGES_SOURCE}>Village boundaries ({fmt(villageCount)})</option>}
                  </select>
                )}
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {FORMATS.map((f) => (
                    <button
                      key={f.value}
                      onClick={() => handleDownload(f.value, f.label)}
                      disabled={download !== null}
                      style={{ fontSize: 12 }}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                {(result.aoi_area_m2 || result.chainage?.labels_on_alignment) && result.count > 0 && !villagesSelected && (
                  <div style={{ marginTop: 8 }}>
                    <button
                      onClick={() => handleDownload("table", downloadSource ? "parcel table" : "parcel tables")}
                      disabled={download !== null}
                      className="primary" style={{ fontSize: 12 }}
                      title="CSV with three columns: total area, area affected, parcel number"
                    >
                      {downloadSource ? "Parcel table (CSV)" : "Parcel tables (one CSV per source file, .zip)"}
                    </button>
                  </div>
                )}
                {(result.villages?.no_parcels?.count ?? 0) > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <button
                      onClick={handleDownloadNoParcels}
                      disabled={download !== null}
                      className="primary" style={{ fontSize: 12 }}
                      title="CSV of the villages found by this search that have no cadastral parcels in the database: names, LGD codes, area and the reason"
                    >
                      Villages without parcels (CSV, {fmt(result.villages!.no_parcels!.count)})
                    </button>
                  </div>
                )}
                {multiSource && !downloadSource && (
                  <label style={{ display: "block", fontSize: 12, marginTop: 6 }}>
                    <input type="checkbox" checked={splitGpkg} onChange={(e) => setSplitGpkg(e.target.checked)} /> GeoPackage: one
                    layer per source file
                  </label>
                )}
                {download && (
                  <ProgressBar
                    fraction={download.fraction ?? 1}
                    label={download.label}
                    indeterminate={download.fraction === null}
                  />
                )}
              </div>
            )}
          </section>
        )}

        <section className="card c-layers">
          <Header
            className="card-head"
            title="Layers"
            info={
              <>
                <P>
                  Tick a layer to show or hide it, use the colour box to change its colour, the slider to fade it, and &#8982; to zoom to
                  it. Parcel layers (one per source file) can be reordered with &#9650;/&#9660;: the top of the list is the top of the map.
                </P>
                <P>
                  <strong>Click a parcel</strong> on the map to see its attributes. Overlapping parcels from the visible layers, and the
                  village it lies in, are listed together.
                </P>
                <P>The basemap is only a background; choose None to hide it.</P>
              </>
            }
          />
          <div style={labelStyle}>Basemap</div>
          <select value={basemapId ?? ""} onChange={(e) => setBasemapId(e.target.value || null)} style={{ width: "100%", marginBottom: 10 }}>
            <option value="">None</option>
            {BASEMAPS.map((b) => (
              <option key={b.id} value={b.id}>
                {b.label}
              </option>
            ))}
          </select>

          <LayerRow
            label="Area of interest"
            color={aoiColor}
            onColor={setAoiColor}
            checked={showAoi}
            onToggle={setShowAoi}
            opacity={aoiOpacity}
            onOpacity={setAoiOpacity}
            onZoom={result ? () => { const b = boundsOf(result.aoi.features); if (b) mapViewRef.current?.fitBounds(b); } : undefined}
          />

          {result?.strip_used && (
            <LayerRow
              label="Strip between the two lines"
              color={stripColor}
              onColor={setStripColor}
              checked={showStrip}
              onToggle={setShowStrip}
              opacity={stripOpacity}
              onOpacity={setStripOpacity}
              onZoom={result.strip ? () => { const b = boundsOf(result.strip!.features); if (b) mapViewRef.current?.fitBounds(b); } : undefined}
            />
          )}
          {result?.aoi_area_m2 && clip && (
            <LayerRow
              label="Parcel parts inside the area"
              title="Each matched parcel cut to the part inside the strip / area"
              detail={fmt(result.clipped_count)}
              color={clipColor}
              onColor={setClipColor}
              checked={showClip}
              onToggle={setShowClip}
              opacity={clipOpacity}
              onOpacity={setClipOpacity}
            />
          )}

          {villageCount > 0 && (
            <LayerRow
              label="Village boundaries"
              title="Village boundary polygons (drawn under the parcels). Click one for its details."
              detail={fmt(villageCount)}
              color={villageColor}
              onColor={setVillageColor}
              checked={showVillages}
              onToggle={setShowVillages}
              opacity={villageOpacity}
              onOpacity={setVillageOpacity}
              onZoom={result?.villages?.bbox ? () => mapViewRef.current?.fitBounds(result.villages!.bbox!) : undefined}
            />
          )}

          {(result?.villages?.no_parcels?.count ?? 0) > 0 && (
            <LayerRow
              label="Villages without parcels"
              title="Villages found by this search that have no cadastral parcels in the database (drawn over the village boundaries). Click one for its details."
              detail={fmt(result!.villages!.no_parcels!.count)}
              color={noParcelsColor}
              onColor={setNoParcelsColor}
              checked={showNoParcels}
              onToggle={setShowNoParcels}
              opacity={noParcelsOpacity}
              onOpacity={setNoParcelsOpacity}
              onZoom={result?.villages?.no_parcels?.bbox ? () => mapViewRef.current?.fitBounds(result.villages!.no_parcels!.bbox!) : undefined}
            />
          )}

          {(chainageLayer ?? chainagePreview) && (
            <LayerRow
              label="Chainage labels"
              title="Chainage labels (text starting with CH or KM) of the chainage DXF, as dots; the text shows from zoom 12. Click one for its value."
              detail={fmt((chainageLayer ?? chainagePreview)!.features.length)}
              color={chainageColor}
              onColor={setChainageColor}
              checked={showChainage}
              onToggle={setShowChainage}
              opacity={chainageOpacity}
              onOpacity={setChainageOpacity}
              onZoom={() => fitTo((chainageLayer ?? chainagePreview)!)}
            />
          )}

          <div style={{ ...labelStyle, marginTop: 10 }}>Parcel layers, one per source file (top of list = top of map)</div>
          {layerOrder.length === 0 && <div style={{ fontSize: 12, color: "#999" }}>None yet.</div>}
          {[...layerOrder].reverse().map((name) => {
            const index = layerOrder.indexOf(name);
            const group = groups[name];
            return (
              <LayerRow
                key={name}
                label={name.replace(/\.parquet$/i, "")}
                title={name}
                detail={fmt(result?.per_source[name] ?? 0)}
                color={colorOf(name)}
                onColor={(c) => setLayerColor((prev) => ({ ...prev, [name]: c }))}
                checked={layerVisible[name] ?? true}
                onToggle={(v) => setLayerVisible((prev) => ({ ...prev, [name]: v }))}
                opacity={opacityOf(name)}
                onOpacity={(v) => setLayerOpacity((prev) => ({ ...prev, [name]: v }))}
                onZoom={group?.bbox ? () => mapViewRef.current?.fitBounds(group.bbox!) : undefined}
                onUp={() => moveLayer(name, 1)}
                onDown={() => moveLayer(name, -1)}
                upDisabled={index === layerOrder.length - 1}
                downDisabled={index === 0}
              />
            );
          })}
        </section>
      </aside>

      <Resizer onResize={resizeSidebar} />

      <div style={{ flex: 1, minWidth: 0 }}>
        <MapView
          ref={mapViewRef}
          basemapId={basemapId}
          aoi={preview ?? result?.aoi ?? null}
          parcelLayers={parcelLayers}
          showAoi={showAoi}
          aoiOpacity={aoiOpacity}
          strip={result?.strip ?? null}
          showStrip={showStrip}
          stripOpacity={stripOpacity}
          clip={clip}
          showClip={showClip}
          clipOpacity={clipOpacity}
          aoiColor={aoiColor}
          stripColor={stripColor}
          clipColor={clipColor}
          villages={villages}
          showVillages={showVillages}
          villageOpacity={villageOpacity}
          villageColor={villageColor}
          villagesNoParcels={villagesNoParcels}
          showNoParcels={showNoParcels}
          noParcelsOpacity={noParcelsOpacity}
          noParcelsColor={noParcelsColor}
          chainage={chainageLayer ?? chainagePreview}
          showChainage={showChainage}
          chainageOpacity={chainageOpacity}
          chainageColor={chainageColor}
        />
      </div>
      </div>
    </div>
  );
}

// What a running search is doing. While it loads a state's parcel data this shows that
// state's progress, since a big state takes minutes.
function JobProgress({ job }: { job: SearchJob | null }) {
  if (!job) return <ProgressBar fraction={1} label="Starting the search..." indeterminate />;
  const loadable = job.states.filter((s) => s.folder);
  const noData = job.states.filter((s) => !s.folder);
  return (
    <div>
      {job.phase !== "loading" && <ProgressBar fraction={1} label={`${job.message}...`} indeterminate />}
      {job.states.length > 0 && (
        <div style={{ fontSize: 11, color: "#666", margin: "4px 0" }}>
          Area falls in: {job.states.map((s) => s.name).join(", ")}
        </div>
      )}
      {loadable.map((s) => (
        <StateLoadProgress key={s.name} state={s} elapsed={job.elapsed} />
      ))}
      {noData.length > 0 && (
        <div style={{ fontSize: 11, color: "#b26a00" }}>No parcel data for {noData.map((s) => s.name).join(", ")}.</div>
      )}
    </div>
  );
}

function StateLoadProgress({ state, elapsed }: { state: JobState; elapsed: number }) {
  const folder = state.folder ?? state.name;
  if (state.status === "queued") return <div style={{ fontSize: 11, color: "#888" }}>{folder}: waiting its turn...</div>;
  if (state.status === "waiting") return <ProgressBar fraction={1} label={`${folder}: another search is loading it, waiting...`} indeterminate />;
  if (state.status === "indexing") return <ProgressBar fraction={1} label={`${folder}: building the spatial index...`} indeterminate />;
  if (state.status === "ready")
    return <div style={{ fontSize: 11, color: "#2e7d32" }}>{folder}: {state.loaded_now ? "loaded" : "already in the database"}</div>;
  const done = state.rows_done ?? 0;
  const total = state.rows_total ?? 0;
  const fraction = total > 0 ? done / total : 0;
  // A rough estimate from the pace so far, once there is enough to go on.
  const eta = done > 200_000 && elapsed > 5 ? ((total - done) / (done / elapsed)) : null;
  return (
    <ProgressBar
      fraction={fraction}
      label={`Loading ${folder}: file ${Math.min((state.files_done ?? 0) + 1, state.files_total ?? 1)}/${state.files_total ?? "?"}, ${fmt(done)} of ${fmt(total)} parcels${eta !== null ? ` (${eta < 60 ? "under a minute" : `about ${fmtDuration(eta)}`} left)` : ""}`}
    />
  );
}

// One state in the "parcel data in the database" list, with its Drop button.
function StateTableRow({
  table,
  secondsLeft,
  onDrop,
  disabled,
}: {
  table: StateTable;
  secondsLeft: number | null;
  onDrop: () => void;
  disabled: boolean;
}) {
  const detail =
    table.status === "loading"
      ? `Loading: file ${table.files_done ?? 0}/${table.files_total ?? "?"}, ${fmt(table.rows_done ?? 0)} of ${fmt(table.rows_total ?? 0)} parcels`
      : table.status === "failed"
        ? `Failed: ${table.error ?? "unknown error"}`
        : `${fmt(table.rows_done ?? 0)} parcels · drops automatically in ${secondsLeft === null ? "?" : fmtDuration(Math.max(0, secondsLeft))}`;
  return (
    <div style={{ padding: "3px 0", borderBottom: "1px solid #f0f0f0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
        <strong style={{ flex: 1 }}>{table.state}</strong>
        <span style={{ fontSize: 11, color: "#888" }}>{fmtBytes(table.size_bytes)}</span>
        <button
          onClick={onDrop}
          disabled={disabled}
          className="mini"
          title={disabled ? "Loading in progress" : "Drop this state's whole parcel table from the database"}
        >
          Drop
        </button>
      </div>
      <div style={{ fontSize: 11, color: table.status === "failed" ? "#b00020" : "#777" }}>{detail}</div>
    </div>
  );
}

interface LayerRowProps {
  label: string;
  title?: string;
  detail?: string;
  color: string;
  /** When given, the colour swatch becomes a colour picker. */
  onColor?: (color: string) => void;
  checked: boolean;
  onToggle: (v: boolean) => void;
  opacity: number;
  onOpacity: (v: number) => void;
  onZoom?: () => void;
  onUp?: () => void;
  onDown?: () => void;
  upDisabled?: boolean;
  downDisabled?: boolean;
}

const arrowStyle = { fontSize: 9, padding: "0 3px", border: "none", background: "none", cursor: "pointer" } as const;

function LayerRow({ label, title, detail, color, onColor, checked, onToggle, opacity, onOpacity, onZoom, onUp, onDown, upDisabled, downDisabled }: LayerRowProps) {
  return (
    <div style={{ padding: "3px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
        <input type="checkbox" checked={checked} onChange={(e) => onToggle(e.target.checked)} />
        {onColor ? (
          <input
            type="color"
            value={color}
            onChange={(e) => onColor(e.target.value)}
            title="Change this layer's colour"
            style={{ width: 22, height: 18, padding: 0, border: "1px solid #bbb", borderRadius: 3, background: "none", cursor: "pointer", flexShrink: 0 }}
          />
        ) : (
          <span style={{ width: 10, height: 10, background: color, display: "inline-block", borderRadius: 2, flexShrink: 0 }} />
        )}
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={title ?? label}>
          {label}
        </span>
        {detail && <span style={{ fontSize: 10, color: "#888" }}>{detail}</span>}
        {onUp && onDown && (
          <span style={{ display: "flex", flexDirection: "column", lineHeight: 0.7 }}>
            <button onClick={onUp} disabled={upDisabled} title="Move up (render on top)" className="plain" style={arrowStyle}>
              &#9650;
            </button>
            <button onClick={onDown} disabled={downDisabled} title="Move down (render below)" className="plain" style={arrowStyle}>
              &#9660;
            </button>
          </span>
        )}
        <button onClick={onZoom} disabled={!onZoom} className="mini" title="Zoom to this layer">
          &#8982;
        </button>
      </div>
      {checked && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: 22 }}>
          <input
            type="range"
            min={0}
            max={100}
            value={Math.round(opacity * 100)}
            onChange={(e) => onOpacity(Number(e.target.value) / 100)}
            style={{ flex: 1, height: 14 }}
            title="Layer opacity"
          />
          <span style={{ fontSize: 10, color: "#888", width: 30, textAlign: "right" }}>{Math.round(opacity * 100)}%</span>
        </div>
      )}
    </div>
  );
}
