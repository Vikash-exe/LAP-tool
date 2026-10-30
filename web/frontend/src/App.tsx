import { useEffect, useMemo, useRef, useState } from "react";
import type { Feature, FeatureCollection } from "geojson";
import MapView, { type MapViewHandle, type ParcelLayer } from "./MapView";
import Resizer from "./Resizer";
import InputFile, { type InputSelection } from "./InputFile";
import PriorityDialog from "./PriorityDialog";
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
  runSearch,
  type AreaStats,
  type InputInfo,
  fetchMarks,
  fetchSources,
  type SourceLayer,
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

const MATCH_INTERSECTING = "Every parcel that overlaps or touches the area (for a line: every parcel it passes through).";
const MATCH_INSIDE = "Only parcels completely contained in the area.";

// With both ticked, the parcels are one search that intersects the area; each parcel is tagged fully inside or not, and the map
// shows the two sets as separate layers.
const INSIDE_SUFFIX = " · fully inside";
const INTERSECT_SUFFIX = " · intersecting";

type DownloadJob = { format: ExportFormat; label: string; clipped?: boolean; data?: "lap" | "source"; combined?: boolean };

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

// The form fields that tell the server what to search: an area, or an alignment with its right of way and start chainage.
function areaFields(a: InputSelection, priority: string[]): Record<string, string> {
  return { kind: "area", area_token: a.token, area_crs: a.crs, ...(priority.length >= 2 ? { priority: JSON.stringify(priority) } : {}) };
}

function linearFields(
  a: InputSelection | null,
  r: InputSelection | null,
  startChainage: string,
  startEnd: "A" | "B",
  step: string,
  priority: string[],
  matchWith: string[]
): Record<string, string> {
  return {
    kind: "linear",
    ...(a ? { align_token: a.token, align_crs: a.crs, start_chainage: startChainage.trim(), start_end: startEnd, chainage_step: step.trim() } : {}),
    ...(r ? { row_token: r.token, row_crs: r.crs } : {}),
    match_with: matchWith.join(","),
    ...(priority.length >= 2 ? { priority: JSON.stringify(priority) } : {}),
  };
}

// The start chainage is metres with at most 3 decimals; the step is a positive number of metres.
const CHAINAGE_INPUT = /^-?\d+(\.\d{1,3})?$/;
const STEP_INPUT = /^\d+(\.\d{1,3})?$/;

const fmtLonLat = (p: [number, number]) => `${p[1].toFixed(5)}, ${p[0].toFixed(5)}`;

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

  // The three inputs. Each file is uploaded and read by the server as soon as it is chosen; what the search would use is
  // drawn on the map (and zoomed to) before anything is searched.
  const [inputKind, setInputKind] = useState<"linear" | "area">("linear");
  const [alignSel, setAlignSel] = useState<InputSelection | null>(null);
  const [rowSel, setRowSel] = useState<InputSelection | null>(null);
  const [areaSel, setAreaSel] = useState<InputSelection | null>(null);
  const [alignPrev, setAlignPrev] = useState<FeatureCollection | null>(null);
  const [rowPrev, setRowPrev] = useState<FeatureCollection | null>(null);
  const [areaPrev, setAreaPrev] = useState<FeatureCollection | null>(null);
  const [alignInfo, setAlignInfo] = useState<InputInfo | null>(null);
  // Linear: the chainage (metres, up to 3 decimals) at one end of the alignment.
  const [startChainage, setStartChainage] = useState("");
  const [startEnd, setStartEnd] = useState<"A" | "B">("A");
  // Linear: a chainage mark every step metres along the alignment, drawn on the map as soon as start chainage and step are known.
  const [chainageStep, setChainageStep] = useState("");
  const [marks, setMarks] = useState<FeatureCollection | null>(null);
  const [marksError, setMarksError] = useState<string | null>(null);
  // The parcel layers (source files) available in the area, and the order of priority the user gave them (highest first).
  const [sources, setSources] = useState<SourceLayer[]>([]);
  const [priority, setPriority] = useState<string[]>([]);
  const [priorityOpen, setPriorityOpen] = useState(false);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  // Match: either or both can be ticked.
  const [matchIntersect, setMatchIntersect] = useState(true);
  const [matchInside, setMatchInside] = useState(false);
  // Alignment search: which element the parcels are matched against (the right of way, the alignment line, or both). Without a
  // right of way file the line is the only choice; a search along the line alone lists the parcels it crosses ("intersecting").
  const [useRow, setUseRow] = useState(true);
  const [useLine, setUseLine] = useState(false);
  const matchWith =
    inputKind === "linear" ? [...((useRow || !alignSel) && rowSel ? ["row"] : []), ...(alignSel && (useLine || !rowSel) ? ["line"] : [])] : [];
  const lineOnly = inputKind === "linear" && !!alignSel && !matchWith.includes("row");
  const mode: MatchMode = lineOnly ? "intersects" : matchIntersect && matchInside ? "both" : matchInside ? "within" : "intersects";

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
  // The parcel CSV: the processed LAP columns and / or the columns of the source files.
  const [tableLap, setTableLap] = useState(true);
  const [tableSource, setTableSource] = useState(false);
  // The polygons: whole parcels and / or the part inside the area, in the formats ticked; CSV (WKT) with LAP and / or source columns.
  const [polyWhole, setPolyWhole] = useState(true);
  const [polyPart, setPolyPart] = useState(false);
  const [polyFormats, setPolyFormats] = useState<ExportFormat[]>([]);
  const [polyCsvLap, setPolyCsvLap] = useState(true);
  const [polyCsvSource, setPolyCsvSource] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

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
  // The chainage marks (the ends A and B before a search, a mark every kilometre after it) are a layer of their own.
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

  useEffect(() => setExportOpen(false), [result?.result_id]); // a new result closes the Export window
  // A new result starts with all of its source layers on, in a fixed order.
  useEffect(() => {
    // bottom of the map first: with a priority the lowest layer is at the bottom and the highest on top
    const order = result?.priority?.order ?? [];
    const rank = (n: string) => (order.indexOf(n) < 0 ? order.length : order.indexOf(n));
    const names0 = Object.keys(result?.per_source ?? {}).sort((a, b) => rank(b) - rank(a) || a.localeCompare(b));
    const names = result?.mode === "both" ? names0.flatMap((n) => [n + INTERSECT_SUFFIX, n + INSIDE_SUFFIX]) : names0;
    setLayerOrder(names);
    setLayerVisible(Object.fromEntries(names.map((n) => [n, true])));
    setLayerOpacity({});
    // With no parcels but some villages, the village layer is the only thing to download.
    setDownloadSource(result && result.count === 0 && (result.villages?.count ?? 0) > 0 ? VILLAGES_SOURCE : "");
    setSplitGpkg(false);
    setPolyWhole(true);
    setPolyPart(false);
  }, [result?.result_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // The layers of this result, highest priority first when a priority was applied, else by name.
  const sourceNames = useMemo(() => {
    const order = result?.priority?.order ?? [];
    const rank = (n: string) => (order.indexOf(n) < 0 ? order.length : order.indexOf(n));
    return Object.keys(result?.per_source ?? {}).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  }, [result?.result_id]); // eslint-disable-line react-hooks/exhaustive-deps
  // The map layers: one per source file, or with both match options two per source (all intersecting parcels, and the fully inside ones on top).
  const both = result?.mode === "both";
  const layerNames = useMemo(
    () => (both ? sourceNames.flatMap((n) => [n + INTERSECT_SUFFIX, n + INSIDE_SUFFIX]) : sourceNames),
    [both, sourceNames]
  );
  const groups = useMemo(() => {
    if (!parcels) return {};
    const bySource = groupBySource(parcels);
    if (!both) return bySource;
    const out: Record<string, SourceGroup> = {};
    for (const [name, g] of Object.entries(bySource)) {
      const inside = g.data.features.filter((f) => f.properties?.fully_inside === true);
      out[name + INTERSECT_SUFFIX] = g;
      out[name + INSIDE_SUFFIX] = { data: { type: "FeatureCollection", features: inside } as FeatureCollection, bbox: boundsOf(inside) };
    }
    return out;
  }, [parcels, both]);
  const colorOf = (name: string) => layerColor[name] ?? PALETTE[Math.max(0, layerNames.indexOf(name)) % PALETTE.length];
  const opacityOf = (name: string) => layerOpacity[name] ?? DEFAULT_PARCEL_OPACITY;

  const parcelLayers: ParcelLayer[] = layerOrder
    .filter((name) => groups[name])
    .map((name) => ({
      id: `s${layerNames.indexOf(name)}`,
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

  async function signOut() {
    await logout();
    window.location.href = "/login";
  }

  function fitTo(fc: FeatureCollection) {
    const b = boundsOf(fc.features);
    if (b) mapViewRef.current?.fitBounds(b);
  }

  async function handleSearch() {
    if (!ready) return;
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
      const res = await runSearch([], mode, [], true, {
        onUploadProgress: setUploadFraction,
        onUploadComplete: () => setPhase("working"),
        onJob: (j) => {
          setJob(j);
          setPhase("working");
        },
      }, inputKind === "area" ? areaFields(areaSel!, priority) : linearFields(alignSel, rowSel, startChainage, startEnd, chainageStep, priority, matchWith));
      setResult(res);
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

  // Downloads the files one after the other. A job is one file: the parcel CSV (combined), or the polygons in one format.
  async function runDownloads(jobs: DownloadJob[]) {
    if (!result) return;
    const villagesLayer = downloadSource === VILLAGES_SOURCE;
    try {
      for (const job of jobs) {
        const split = !job.combined && splitGpkg && job.format === "gpkg" && !downloadSource;
        const clipped = !!job.clipped && !!result.aoi_area_m2;
        const what = job.combined ? "all layers" : villagesLayer ? "village boundaries" : split ? "one layer per source" : downloadSource || "all sources";
        setDownload({ label: `Preparing ${job.label} (${what}${clipped ? ", part inside" : ""})...`, fraction: null });
        await downloadResult(
          result.result_id,
          job.format,
          {
            source: job.combined || villagesLayer ? null : downloadSource || null,
            split,
            clipped,
            villages: !job.combined && villagesLayer,
            data: job.data,
            combined: job.combined,
          },
          (fraction) => setDownload({ label: `Downloading ${job.label}`, fraction })
        );
      }
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
    setResult(null);
    setParcels(null);
    setClip(null);
    setVillages(null);
    setVillagesNoParcels(null);
    setChainageLayer(null);
    setError(null);
  }

  const busy = phase !== "idle";
  const startChainageOk = CHAINAGE_INPUT.test(startChainage.trim());
  const stepOk = STEP_INPUT.test(chainageStep.trim()) && Number(chainageStep) > 0;
  const ready =
    (lineOnly || matchIntersect || matchInside) &&
    (inputKind === "area"
      ? !!areaSel
      : alignSel
        ? startChainageOk && (chainageStep.trim() === "" || stepOk)
        : !!rowSel); // without an alignment file the right of way alone is searched and there is no chainage
  // What the search is still waiting for, shown under the Find parcels button.
  const missing = [
    ...(inputKind === "area"
      ? [!areaSel && "the area of interest file"]
      : alignSel
        ? [!startChainageOk && "the start chainage", chainageStep.trim() !== "" && !stepOk && "a valid chainage step"]
        : [!rowSel && "the alignment file or the right of way file"]),
    !lineOnly && !matchIntersect && !matchInside && "a match option",
  ].filter(Boolean) as string[];
  const preview = useMemo<FeatureCollection | null>(() => {
    const parts = inputKind === "area" ? [areaPrev] : [rowPrev, alignPrev];
    const features = parts.flatMap((p) => p?.features ?? []);
    return features.length > 0 ? { type: "FeatureCollection", features } : null;
  }, [inputKind, areaPrev, rowPrev, alignPrev]);
  // The chainage marks (calculated as soon as the start chainage and step are given), and the two ends of the alignment on top.
  useEffect(() => {
    setMarksError(null);
    if (inputKind !== "linear" || !alignSel || !startChainageOk || !stepOk) {
      setMarks(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      fetchMarks({ token: alignSel.token, crs: alignSel.crs, startChainage: startChainage.trim(), startEnd, step: chainageStep.trim() })
        .then((fc) => !cancelled && setMarks(fc))
        .catch((e) => {
          if (cancelled) return;
          setMarks(null);
          setMarksError(e instanceof Error ? e.message : String(e));
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [inputKind, alignSel, startChainage, startEnd, chainageStep]); // eslint-disable-line react-hooks/exhaustive-deps
  // A right of way file chosen: match against it (as before); removed: only the alignment line is left.
  useEffect(() => {
    setUseRow(!!rowSel);
    setUseLine(!rowSel);
  }, [!!rowSel]); // eslint-disable-line react-hooks/exhaustive-deps
  // The parcel layers available where the area (or the right of way) lies: with two or more, a small window asks for their priority.
  useEffect(() => {
    const sel = inputKind === "area" ? areaSel : rowSel ?? alignSel; // the area searched: the right of way, else the alignment line itself
    setSourcesError(null);
    if (!sel) {
      setSources([]);
      setPriority([]);
      return;
    }
    let cancelled = false;
    fetchSources(
      inputKind === "area"
        ? { kind: "area", area_token: sel.token, area_crs: sel.crs }
        : rowSel
        ? { kind: "linear", row_token: rowSel.token, row_crs: rowSel.crs }
        : { kind: "linear", align_token: sel.token, align_crs: sel.crs }
    )
      .then((r) => {
        if (cancelled) return;
        const names = r.sources.map((s) => s.source);
        setSources(r.sources);
        // keep the order already given for layers that are still there; new ones go last
        setPriority((prev) => {
          const kept = prev.filter((n) => names.includes(n));
          return [...kept, ...names.filter((n) => !kept.includes(n))];
        });
        if (names.length >= 2) setPriorityOpen(true);
      })
      .catch((e) => {
        if (cancelled) return;
        setSources([]);
        setPriority([]);
        setSourcesError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [inputKind, areaSel, rowSel, alignSel]);
  const chainagePreview = useMemo<FeatureCollection | null>(() => {
    const ends = inputKind === "linear" ? alignInfo?.ends : undefined;
    const features: Feature[] = [...(inputKind === "linear" ? marks?.features ?? [] : [])];
    if (ends) {
      (["A", "B"] as const).forEach((e) =>
        features.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: ends[e] },
          properties: { text: startEnd === e ? `${e} (start)` : e, end: true },
        })
      );
    }
    return features.length > 0 ? { type: "FeatureCollection", features } : null;
  }, [inputKind, alignInfo, startEnd, marks]);
  const multiSource = sourceNames.length > 1;
  const villageCount = result?.villages?.count ?? 0;

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
                  Choose what to search along. <strong>Alignment</strong> is a route: you give the centreline and the right of way, and the
                  parcels along it get a chainage. <strong>Area</strong> is a closed polygon.
                </P>
                <P>
                  Every file can be a <strong>.kml</strong>, <strong>.kmz</strong>, <strong>.geojson</strong>, <strong>.gpkg</strong>, a zipped
                  shapefile (<strong>.zip</strong>, or the parts .shp + .dbf + .shx + .prj selected together) or a <strong>.dxf</strong>. A DXF does
                  not say which coordinate system it uses, so you choose it after the upload. Elevation is ignored, and arcs are kept as drawn.
                </P>
                <P>
                  Each file is read as soon as it is chosen and drawn on the map, so you can check it is the right one before searching.
                </P>
              </>
            }
          />
          <div style={{ fontSize: 13, marginBottom: 8, fontWeight: "bold" }}>
            <label style={{ marginRight: 16 }}>
              <input type="radio" name="inputkind" checked={inputKind === "linear"} onChange={() => setInputKind("linear")} disabled={busy} /> Alignment
            </label>
            <label>
              <input type="radio" name="inputkind" checked={inputKind === "area"} onChange={() => setInputKind("area")} disabled={busy} /> Area
            </label>
          </div>

          <div style={{ display: inputKind === "linear" ? "block" : "none" }}>
            <InputFile
              role="alignment"
              title="Alignment file (the centreline)"
              info={
                <>
                  <P>
                    The alignment is <strong>one centreline</strong>. If the line is drawn in several pieces whose ends meet, the pieces are joined
                    into one. If the file holds other lines too, the longest one is used: remove the others if that is not the line you mean.
                  </P>
                  <P>
                    The two ends of the centreline are marked <strong>A</strong> (where the line starts in the file) and <strong>B</strong> (where it
                    ends) on the map. The start chainage below belongs to the end you choose.
                  </P>
                </>
              }
              disabled={busy}
              onSelection={setAlignSel}
              onPreview={(fc, info) => {
                setAlignPrev(fc);
                setAlignInfo(info);
                if (fc) setShowAoi(true);
              }}
              onFit={fitTo}
              summary={(i) =>
                `One centreline of ${(((i.length_m ?? 0) / 1000)).toFixed(3)} km` +
                ((i.pieces ?? 0) > (i.lines_found ?? 0) ? ` (${fmt(i.pieces ?? 0)} pieces joined)` : "") +
                ((i.ignored ?? 0) > 0 ? `; ${fmt(i.ignored ?? 0)} other line${i.ignored === 1 ? "" : "s"} ignored.` : ".")
              }
            />
            <InputFile
              role="row"
              title="Right of way (RoW) file (optional)"
              info={
                <>
                  <P>
                    The right of way is either <strong>two lines</strong> (its two edges) or <strong>one closed polygon</strong>. With two lines
                    the strip between them is the right of way, and each parcel's area inside it is measured. With a polygon, the polygon itself is
                    searched.
                  </P>
                  <P>
                    In a DXF the longest closed outline is taken as the polygon; without one, the two longest lines. If the file holds more, remove
                    the extra long lines or outlines first.
                  </P>
                </>
              }
              disabled={busy}
              onSelection={setRowSel}
              onPreview={(fc) => {
                setRowPrev(fc);
                if (fc) setShowAoi(true);
              }}
              onFit={fitTo}
              summary={(i) =>
                i.kind === "two_lines"
                  ? `Two lines (${(i.lengths_m ?? []).map((m) => `${(m / 1000).toFixed(2)} km`).join(" and ")}): the strip between them.`
                  : `A closed polygon${(i.count ?? 1) > 1 ? ` (${i.count} of them)` : ""}.`
              }
            />

            {!alignSel && (
              <div style={{ fontSize: 11, color: "#666", marginTop: 6 }}>
                No alignment file: only the right of way is searched, and the chainage is skipped. Add an alignment file to get chainage.
              </div>
            )}
            {alignSel && <span style={labelStyle}>Start chainage (metres)</span>}
            {alignSel && <input
              type="text"
              inputMode="decimal"
              placeholder="e.g. 12500.000"
              value={startChainage}
              onChange={(e) => setStartChainage(e.target.value)}
              disabled={busy}
              style={{ width: "100%", fontSize: 13, boxSizing: "border-box" }}
            />}
            {alignSel && startChainage.trim() !== "" && !startChainageOk && (
              <div style={{ fontSize: 11, color: "#b00020", marginTop: 3 }}>Enter metres with up to 3 decimals, for example 12500.000 (or -250.5).</div>
            )}
            {startChainageOk && alignSel && (
              <>
                <span style={labelStyle}>Chainage step (metres)</span>
                <input
                  type="text"
                  inputMode="decimal"
                  placeholder="e.g. 100"
                  value={chainageStep}
                  onChange={(e) => setChainageStep(e.target.value)}
                  disabled={busy}
                  style={{ width: "100%", fontSize: 13, boxSizing: "border-box" }}
                />
                {chainageStep.trim() !== "" && !stepOk && (
                  <div style={{ fontSize: 11, color: "#b00020", marginTop: 3 }}>Enter the step in metres, more than 0, for example 100.</div>
                )}
                {marksError && <div style={{ fontSize: 11, color: "#b00020", marginTop: 3 }}>{marksError}</div>}
                {marks && (
                  <div style={{ fontSize: 11, color: "#666", marginTop: 3 }}>
                    {fmt(marks.features.length)} chainage marks are drawn on the map (CH: 12+100, and KM: 13+000 at the start, the end and every whole kilometre) (the text shows when you zoom in).
                  </div>
                )}
                {!marks && !marksError && !stepOk && (
                  <div style={{ fontSize: 11, color: "#666", marginTop: 3 }}>
                    Give the step to see the chainage marks (a start, then every multiple of the step, then the end) along the alignment.
                  </div>
                )}
              </>
            )}
            {!alignSel ? null : alignInfo?.ends ? (
              <>
                <span style={labelStyle}>The start chainage is at</span>
                <label style={{ display: "block", fontSize: 12, padding: "1px 0" }}>
                  <input type="radio" name="startend" checked={startEnd === "A"} onChange={() => setStartEnd("A")} disabled={busy} /> end <strong>A</strong>{" "}
                  <span style={{ color: "#666" }}>({fmtLonLat(alignInfo.ends.A)})</span>
                </label>
                <label style={{ display: "block", fontSize: 12, padding: "1px 0" }}>
                  <input type="radio" name="startend" checked={startEnd === "B"} onChange={() => setStartEnd("B")} disabled={busy} /> end <strong>B</strong>{" "}
                  <span style={{ color: "#666" }}>({fmtLonLat(alignInfo.ends.B)})</span>
                </label>
                <div style={{ fontSize: 11, color: "#666", marginTop: 3 }}>
                  Chainage increases from the start along the centreline. Both ends are marked on the map (latitude, longitude shown).
                </div>
              </>
            ) : (
              <div style={{ fontSize: 11, color: "#666", marginTop: 3 }}>Once the alignment is read, you choose which end the start chainage belongs to.</div>
            )}
          </div>

          <div style={{ display: inputKind === "area" ? "block" : "none" }}>
            <InputFile
              role="area"
              title="Area of interest file"
              info={
                <P>
                  The area of interest is a <strong>closed polygon</strong>. Each parcel's area inside it is measured. For a DXF, the largest closed
                  outline in the file is used.
                </P>
              }
              disabled={busy}
              onSelection={setAreaSel}
              onPreview={(fc) => {
                setAreaPrev(fc);
                if (fc) setShowAoi(true);
              }}
              onFit={fitTo}
              summary={(i) => `A closed polygon${(i.count ?? 1) > 1 ? ` (${i.count} of them)` : ""}.`}
            />
          </div>
        </section>

        <section className="card c-match">
          <Header
            className="card-head"
            title="2. Match"
            info={
              <>
                <P>Which parcels count as matching the area. Tick one or both:</P>
                <P>
                  <strong>Intersecting</strong>: {MATCH_INTERSECTING}
                </P>
                <P>
                  <strong>Fully inside</strong>: {MATCH_INSIDE}
                </P>
                <P>
                  With <strong>both</strong> ticked, the map shows two layers for each source file: all the intersecting parcels, and on top of them
                  the ones that are fully inside (switch either off in Layers). The tables and downloads hold all the intersecting parcels. The same rule is
                  applied to the village boundaries.
                </P>
                <P>
                  When the area has two or more parcel layers (source files), a window asks you to put them in order of priority. Every parcel of the top layer
                  is kept; from each layer below, only the parcels not already covered by the layers above are added. Downloads, tables and the layer list follow
                  that order.
                </P>
              </>
            }
          />
          {inputKind === "linear" && (
            <div style={{ marginBottom: 8 }}>
              <span style={labelStyle}>Match the parcels against</span>
              <label style={{ display: "block", fontSize: 13, padding: "2px 0" }} title="The parcels the alignment centreline crosses">
                <input
                  type="checkbox"
                  checked={matchWith.includes("line")}
                  disabled={busy || !rowSel || !alignSel}
                  onChange={(e) => (e.target.checked || (useRow && !!rowSel) ? setUseLine(e.target.checked) : undefined)}
                />{" "}
                Alignment line
              </label>
              <label style={{ display: "block", fontSize: 13, padding: "2px 0" }} title={rowSel ? "The parcels inside or touching the right of way" : "Choose a right of way file first"}>
                <input
                  type="checkbox"
                  checked={matchWith.includes("row")}
                  disabled={busy || !rowSel || !alignSel}
                  onChange={(e) => (e.target.checked || useLine ? setUseRow(e.target.checked) : undefined)}
                />{" "}
                Right of way{!rowSel ? " (no file chosen)" : ""}
              </label>
            </div>
          )}
          <label style={{ display: "block", fontSize: 13, padding: "2px 0" }} title={MATCH_INTERSECTING}>
            <input type="checkbox" checked={matchIntersect} onChange={(e) => setMatchIntersect(e.target.checked)} disabled={busy} /> Intersecting
          </label>
          <label style={{ display: "block", fontSize: 13, padding: "2px 0" }} title={MATCH_INSIDE}>
            <input type="checkbox" checked={matchInside} onChange={(e) => setMatchInside(e.target.checked)} disabled={busy} /> Fully inside
          </label>
          {lineOnly && (
            <div style={{ fontSize: 11, color: "#666", marginTop: 3 }}>
              Matched against the alignment line alone: every parcel the line crosses is listed, with the chainage where the line enters and leaves it.
            </div>
          )}
          {!lineOnly && !matchIntersect && !matchInside && <div style={{ fontSize: 11, color: "#b00020", marginTop: 3 }}>Tick at least one.</div>}
          {sources.length >= 2 && (
            <div style={{ marginTop: 8, fontSize: 12 }}>
              <span style={labelStyle}>Layer priority</span>
              <div style={{ color: "#444", marginBottom: 4 }}>{priority.map((n, i) => `${i + 1}. ${n.replace(/\.parquet$/i, "")}`).join("  ›  ")}</div>
              <button type="button" className="mini" onClick={() => setPriorityOpen(true)} disabled={busy}>
                Change priority
              </button>
            </div>
          )}
          {sourcesError && <div style={{ fontSize: 11, color: "#b00020", marginTop: 3 }}>{sourcesError}</div>}

          <button
            onClick={handleSearch}
            disabled={!ready || busy || !!backendError}
            className="primary big" style={{ marginTop: 12 }}
          >
            Find parcels
          </button>
          {!ready && !busy && missing.length > 0 && (
            <div style={{ fontSize: 11, color: "#7a4a00", marginTop: 6 }}>Still needed to search: {missing.join(", ")}.</div>
          )}
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
                    those villages; <em>Affected ha</em> is the part of them within your area.
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
              {result.mode === "both" ? `${fmt(result.fully_inside)} fully inside · ` : ""}found in {result.seconds}s{multiSource ? ` · ${sourceNames.length} source layers` : ""}
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
            {result.chainage && !result.chainage.error && result.chainage.kind === "linear" && (
              <div style={{ fontSize: 12, margin: "2px 0 6px" }}>
                <span style={{ color: "#666" }}>Chainage: </span>
                <span style={{ color: chainageColor, fontWeight: "bold" }}>
                  {result.chainage.first} to {result.chainage.last}
                </span>{" "}
                ({((result.chainage.alignment_length_m ?? 0) / 1000).toFixed(3)} km from end {result.chainage.start_end}); the parcel table gets
                chainage from/to
              </div>
            )}
            {result.chainage && !result.chainage.error && result.chainage.kind !== "linear" && (
              <div style={{ fontSize: 12, margin: "2px 0 6px" }}>
                <span style={{ color: "#666" }}>Chainage: </span>
                <span style={{ color: chainageColor, fontWeight: "bold" }}>
                  {result.chainage.first} to {result.chainage.last}
                </span>{" "}
                ({fmt(result.chainage.labels_on_alignment ?? 0)} labels on the alignment); the parcel table gets chainage from/to and the labels inside each parcel
              </div>
            )}
            {result.chainage?.error && (
              <div style={{ fontSize: 12, margin: "2px 0 6px", color: "#b00020" }}>Chainage could not be applied: {result.chainage.error}</div>
            )}
            {result.priority && (
              <div style={{ fontSize: 12, margin: "2px 0 6px" }}>
                <span style={{ color: "#666" }}>Layer priority: </span>
                {result.priority.order.map((n, i) => `${i + 1}. ${n.replace(/\.parquet$/i, "")}`).join("  ›  ")}
                {Object.keys(result.priority.dropped).length > 0 && (
                  <span style={{ color: "#666" }}>
                    {" "}
                    ({Object.entries(result.priority.dropped)
                      .map(([n, k]) => `${fmt(k)} from ${n.replace(/\.parquet$/i, "")}`)
                      .join(", ")}{" "}
                    left out: already covered by a higher layer)
                  </span>
                )}
              </div>
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
                            <th style={{ fontWeight: "normal" }} title="Part of them inside the area">Affected ha</th>
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
                        <strong>Parcel ha</strong>: the whole parcels that touch the area. <strong>Affected ha</strong>: only the part of
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
                      <th style={{ fontWeight: "normal" }} title="Part of those parcels lying inside the area">Affected ha</th>
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
              <button type="button" className="primary big" style={{ marginTop: 12, width: "100%" }} onClick={() => setExportOpen(true)}>
                Export
              </button>
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
              title="Chainage marks along the alignment: a mark at the start, at every multiple of the step and at the end, labelled CH: 12+100, or KM: 13+000 at the start, the end and every whole kilometre, plus its two ends A and B. They appear as soon as the start chainage and step are given; the text shows from zoom 12."
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
                label={name.replace(/\.parquet(?= ·|$)/i, "")}
                title={name}
                detail={fmt(group?.data.features.length ?? result?.per_source[name] ?? 0)}
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

      {exportOpen && result && (result.count > 0 || villageCount > 0) && (
        <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && setExportOpen(false)}>
          <div className="modal modal-wide" role="dialog" aria-label="Export">
            <div className="modal-head">Export</div>
            <div className="modal-body">
                <Header
                  level="sub"
                  title="What to export"
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
                          <strong>Parcel table (CSV)</strong>: <strong>LAP</strong> has the columns <em>Parcel ID</em>, <em>Total Area</em> (whole parcel),{" "}
                          <em>Affected Area</em> (the part inside the {result.strip_used ? "strip" : "area"}), {(result.chainage?.labels_on_alignment ?? 0) > 0 ? <><em>Chainage From</em>, <em>Chainage To</em>, </> : null}
                          <em>Village Name</em>, <em>Taluka Name</em>, <em>District Name</em> and <em>State Name</em> (from the state boundary file); areas are in square
                          metres and parcels that only touch the edge are left out. <strong>Source</strong> has the columns of the source files instead (with the source file name first). The same
                          choice applies to the polygons' CSV (WKT). The parcel CSV holds every layer searched in one file, one parcel per place, from the highest-priority layer.
                        </P>
                      )}
                      {(result.chainage?.labels_on_alignment ?? 0) > 0 && result.chainage?.kind === "linear" && (
                        <P>
                          <strong>Chainage</strong>: the parcel tables get <em>chainage from</em> and <em>chainage to</em>: where the part of the parcel inside
                          the right of way begins and ends along the centreline, as the start chainage plus the distance along it (to 3 decimals). The rows are
                          in order of increasing chainage.
                        </P>
                      )}
                      {(result.chainage?.labels_on_alignment ?? 0) > 0 && result.chainage?.kind !== "linear" && (
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
                {(result.aoi_area_m2 || result.chainage?.labels_on_alignment) && result.count > 0 && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>Parcel table (CSV)</div>
                    <div style={{ fontSize: 12, marginBottom: 6 }}>
                      <label style={{ marginRight: 12 }} title="Parcel ID, Total Area, Affected Area, Chainage From / To, Village, Taluka, District and State Name">
                        <input type="checkbox" checked={tableLap} onChange={(e) => setTableLap(e.target.checked)} /> LAP
                      </label>
                      <label title="The columns of the source files the parcels come from">
                        <input type="checkbox" checked={tableSource} onChange={(e) => setTableSource(e.target.checked)} /> Source
                      </label>
                    </div>
                    <button
                      onClick={() =>
                        runDownloads([
                          ...(tableLap ? [{ format: "table" as ExportFormat, label: "LAP parcel CSV", data: "lap" as const, combined: true }] : []),
                          ...(tableSource ? [{ format: "table" as ExportFormat, label: "source parcel CSV", data: "source" as const, combined: true }] : []),
                        ] as DownloadJob[])
                      }
                      disabled={download !== null || (!tableLap && !tableSource)}
                      className="primary"
                      style={{ fontSize: 12 }}
                      title={
                        multiSource
                          ? "One CSV with the parcels of all the layers searched: one parcel per place, taken from the highest-priority layer"
                          : "One CSV with the parcels found"
                      }
                    >
                      {tableLap && tableSource ? "Download LAP + Source Parcel CSV" : tableSource ? "Download Source Parcel CSV" : "Download LAP Parcel CSV"}
                    </button>
                    {multiSource && (
                      <div style={{ fontSize: 11, color: "#666", marginTop: 3 }}>
                        All layers in one file: one parcel per place, from the highest-priority layer.
                      </div>
                    )}
                  </div>
                )}
                <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>Parcel polygons</div>
                {result.aoi_area_m2 && (
                  <div style={{ fontSize: 12, marginBottom: 6 }}>
                    <label style={{ marginRight: 12 }} title="Each parcel as it is, with its area columns">
                      <input type="checkbox" checked={polyWhole} onChange={(e) => setPolyWhole(e.target.checked)} /> Whole parcels
                    </label>
                    <label title="Each parcel cut to the part inside the area (a parcel cut into several pieces has one polygon per piece)">
                      <input type="checkbox" checked={polyPart} onChange={(e) => setPolyPart(e.target.checked)} /> Only the part inside
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
                    {result.count > 0 && (
                      <option value="">
                        {result.priority ? "All layers together, in priority order" : "All parcel source layers together"} ({fmt(result.count)})
                      </option>
                    )}
                    {sourceNames.map((name, i) => (
                      <option key={name} value={name}>
                        {result.priority ? `${i + 1}. ` : ""}
                        {name} ({fmt(result.per_source[name])})
                      </option>
                    ))}
                    {villageCount > 0 && <option value={VILLAGES_SOURCE}>Village boundaries ({fmt(villageCount)})</option>}
                  </select>
                )}
                <div style={{ fontSize: 12, marginBottom: 4, color: "#666" }}>File format</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px", fontSize: 12 }}>
                  {FORMATS.map((f) => (
                    <label key={f.value}>
                      <input
                        type="checkbox"
                        checked={polyFormats.includes(f.value)}
                        onChange={(e) => setPolyFormats(e.target.checked ? [...polyFormats, f.value] : polyFormats.filter((v) => v !== f.value))}
                      />{" "}
                      {f.label}
                    </label>
                  ))}
                </div>
                {polyFormats.includes("csv") && (
                  <div style={{ fontSize: 12, marginTop: 4 }}>
                    <span style={{ color: "#666", marginRight: 8 }}>CSV (WKT) columns:</span>
                    <label style={{ marginRight: 12 }} title="Parcel ID, Total Area, Affected Area, Chainage From / To, Village, Taluka, District and State Name">
                      <input type="checkbox" checked={polyCsvLap} onChange={(e) => setPolyCsvLap(e.target.checked)} /> LAP
                    </label>
                    <label title="The columns of the source file the parcels come from">
                      <input type="checkbox" checked={polyCsvSource} onChange={(e) => setPolyCsvSource(e.target.checked)} /> Source
                    </label>
                  </div>
                )}
                {(() => {
                  const geometries: boolean[] = result.aoi_area_m2 ? [...(polyWhole ? [false] : []), ...(polyPart ? [true] : [])] : [false];
                  const jobs: DownloadJob[] = geometries.flatMap((clipped) =>
                    polyFormats.flatMap((f): DownloadJob[] => {
                      const label = FORMATS.find((x) => x.value === f)!.label;
                      if (f !== "csv") return [{ format: f, label, clipped }];
                      return [
                        ...(polyCsvLap ? [{ format: f, label: `${label}, LAP columns`, clipped, data: "lap" as const }] : []),
                        ...(polyCsvSource ? [{ format: f, label: `${label}, source columns`, clipped, data: "source" as const }] : []),
                      ];
                    })
                  );
                  return (
                    <div style={{ marginTop: 8 }}>
                      <button
                        onClick={() => runDownloads(jobs)}
                        disabled={download !== null || jobs.length === 0}
                        className="primary"
                        style={{ fontSize: 12 }}
                        title="Downloads one file for each format ticked (and for each of whole parcels / part inside)"
                      >
                        {jobs.length > 1 ? `Download polygons (${jobs.length} files)` : "Download polygons"}
                      </button>
                      {jobs.length === 0 && <span style={{ fontSize: 11, color: "#666", marginLeft: 8 }}>Tick at least one file format.</span>}
                    </div>
                  );
                })()}
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
            <div className="modal-foot">
              <span style={{ flex: 1 }} />
              <button type="button" className="primary" onClick={() => setExportOpen(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}      {priorityOpen && sources.length >= 2 && (
        <PriorityDialog sources={sources} order={priority} onChange={setPriority} onClose={() => setPriorityOpen(false)} />
      )}
      <Resizer onResize={resizeSidebar} />

      <div style={{ flex: 1, minWidth: 0 }}>
        <MapView
          ref={mapViewRef}
          basemapId={basemapId}
          aoi={result ? result.aoi : preview}
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
