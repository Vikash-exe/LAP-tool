import type { FeatureCollection } from "geojson";

// Relative on purpose: the Vite dev server proxies /api to the backend.
const BASE = "";

export type ProgressCallback = (fraction: number) => void;
export type BBox = [number, number, number, number];
export type MatchMode = "intersects" | "within" | "crossing";
/** "table" is the three-column CSV: total area, area affected, parcel number. */
export type ExportFormat = "geojson" | "gpkg" | "shp" | "kml" | "csv" | "table";

/** A state with parcel files on the mount; its parcels are loaded into the database only when a search needs them. */
export interface AvailableState {
  state: string;
  files: number;
  parquet_bytes: number;
  /** Expected size in the database once loaded. */
  est_bytes: number;
  status: "not_loaded" | "loading" | "ready" | "failed";
}

/** A state whose parcel table is in the database (or being loaded). */
export interface StateTable {
  state: string;
  table_name: string;
  status: "loading" | "ready" | "failed";
  files_total: number | null;
  files_done: number | null;
  rows_total: number | null;
  rows_done: number | null;
  size_bytes: number;
  loaded_at: string | null;
  last_used: string | null;
  /** Seconds until the table is dropped automatically (null while loading). */
  expires_in_seconds: number | null;
  error: string | null;
}

export interface StateTables {
  tables: StateTable[];
  /** How long a table lives after it was last used. */
  ttl_seconds: number;
  budget_bytes: number;
  in_use_bytes: number;
}

/** A state the searched area touches, with the progress of loading its parcel data. */
export interface JobState {
  name: string;
  /** Parcel folder name; null when there is no parcel data for this state. */
  folder: string | null;
  inside: boolean;
  status: "queued" | "no_data" | "waiting" | "loading" | "indexing" | "ready";
  files_done?: number | null;
  files_total?: number | null;
  rows_done?: number | null;
  rows_total?: number | null;
  current_file?: string;
  /** True when this search loaded the state (it was not in the database before). */
  loaded_now?: boolean;
  /** Why there is no parcel data, when status is "no_data". */
  note?: string;
}

export interface SearchJob {
  id: string;
  status: "running" | "done" | "error";
  phase: "queued" | "reading" | "locating" | "loading" | "searching" | "finishing" | "done" | "error";
  message: string;
  states: JobState[];
  error: string | null;
  error_status: number | null;
  response: QueryResult | null;
  elapsed: number;
}

/** Counts and areas (square metres, on the ellipsoid) for a group of parcels. */
export interface AreaStats {
  count: number;
  /** Sum of the parcels' full areas. */
  area_m2: number;
  /** Sum of the parts of those parcels that fall inside the area of interest. */
  inside_m2: number;
}

/** The village boundaries that match the same area; a layer of its own next to the parcels. */
export interface VillageSummary {
  count: number;
  per_state?: Record<string, number>;
  stats?: { total: AreaStats; per_state: Record<string, AreaStats> };
  clipped_count?: number;
  bbox?: BBox | null;
  seconds?: number;
  /** Set when the village search could not run (e.g. too many villages match). */
  error?: string;
}

export interface QueryResult {
  result_id: string;
  /** The states the area falls in, and whether this search had to load them. */
  detected_states: JobState[];
  state_folders: string[];
  /** null when no village data is loaded. */
  villages: VillageSummary | null;
  mode: MatchMode;
  states: string[];
  count: number;
  per_state: Record<string, number>;
  per_source: Record<string, number>;
  stats: { total: AreaStats; per_state: Record<string, AreaStats>; per_source: Record<string, AreaStats> };
  /** True when the upload was exactly two lines and the strip between them was used. */
  strip_used: boolean;
  /** The strip polygon (only when strip_used). */
  strip: FeatureCollection | null;
  /** Area of the polygon/strip searched; null for lines and points. */
  aoi_area_m2: number | null;
  clipped_count: number;
  bbox: BBox | null;
  seconds: number;
  aoi: FeatureCollection;
}

async function errorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    return typeof body.detail === "string" ? body.detail : fallback;
  } catch {
    return `${fallback} (${res.status})`;
  }
}

export async function getStates(): Promise<{
  states: AvailableState[];
  max_features: number;
  ttl_seconds: number;
  budget_bytes: number;
}> {
  const res = await fetch(`${BASE}/api/states`);
  if (!res.ok) throw new Error(await errorMessage(res, "Backend unavailable"));
  return res.json();
}

export async function getStateTables(): Promise<StateTables> {
  const res = await fetch(`${BASE}/api/state-tables`);
  if (!res.ok) throw new Error(await errorMessage(res, "Could not read the loaded states"));
  return res.json();
}

/** Drops one state's whole parcel table from the database. */
export async function dropStateTable(state: string): Promise<{ state: string; freed_bytes: number }> {
  const res = await fetch(`${BASE}/api/state-tables/${encodeURIComponent(state)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await errorMessage(res, `Could not drop ${state}`));
  return res.json();
}

/** Drops every loaded state table that no running search is using. */
export async function dropAllStateTables(): Promise<{
  dropped: { state: string; freed_bytes: number }[];
  skipped: { state: string; reason: string }[];
  freed_bytes: number;
}> {
  const res = await fetch(`${BASE}/api/state-tables`, { method: "DELETE" });
  if (!res.ok) throw new Error(await errorMessage(res, "Could not clear the loaded states"));
  return res.json();
}

// fetch() has no upload-progress event; XMLHttpRequest still does.
// `onUploadComplete` fires once every byte has left the browser. The server then starts
// the search as a background job (it may first have to load a state's parcel data, which
// takes minutes for a big state) and answers with its id.
function startQuery(
  files: File[],
  mode: MatchMode,
  states: string[],
  strip: boolean,
  onProgress?: ProgressCallback,
  onUploadComplete?: () => void
): Promise<{ job_id: string }> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("mode", mode);
  form.append("states", states.join(","));
  form.append("strip", String(strip));

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/query`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => onUploadComplete?.();
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.response));
      } else {
        try {
          reject(new Error(JSON.parse(xhr.response).detail ?? `Search failed (${xhr.status})`));
        } catch {
          reject(new Error(`Search failed (${xhr.status})`));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Search failed (network error)"));
    xhr.send(form);
  });
}

export async function getJob(jobId: string): Promise<SearchJob> {
  const res = await fetch(`${BASE}/api/jobs/${jobId}`);
  if (!res.ok) throw new Error(await errorMessage(res, "Lost track of the search"));
  return res.json();
}

export interface SearchCallbacks {
  onUploadProgress?: ProgressCallback;
  onUploadComplete?: () => void;
  /** Called on every poll, so the dashboard can show what the search is doing (e.g. loading a state). */
  onJob?: (job: SearchJob) => void;
}

/** Uploads the area, then follows the search job until it finishes. */
export async function runSearch(
  files: File[],
  mode: MatchMode,
  states: string[],
  strip: boolean,
  callbacks: SearchCallbacks = {}
): Promise<QueryResult> {
  const { job_id } = await startQuery(files, mode, states, strip, callbacks.onUploadProgress, callbacks.onUploadComplete);
  for (;;) {
    const job = await getJob(job_id);
    callbacks.onJob?.(job);
    if (job.status === "done" && job.response) return job.response;
    if (job.status === "error") throw new Error(job.error ?? "The search failed");
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

/** The matched parcels; with clipped=true, only the part of each inside the area of interest. */
export async function fetchParcels(resultId: string, clipped = false): Promise<FeatureCollection> {
  const res = await fetch(`${BASE}/api/results/${resultId}/features${clipped ? "?clipped=true" : ""}`);
  if (!res.ok) throw new Error(await errorMessage(res, "Could not load the parcels"));
  return res.json();
}

/** The village boundaries matched by the search. */
export async function fetchVillages(resultId: string): Promise<FeatureCollection> {
  const res = await fetch(`${BASE}/api/results/${resultId}/features?layer=villages`);
  if (!res.ok) throw new Error(await errorMessage(res, "Could not load the village boundaries"));
  return res.json();
}

// Streams the body so progress can be shown when the server sends a
// Content-Length; otherwise `onProgress` is never called and the caller
// shows an indeterminate bar.
async function fetchBlob(url: string, onProgress?: ProgressCallback): Promise<{ blob: Blob; filename: string | null }> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await errorMessage(res, "Download failed"));

  const filename = /filename="?([^";]+)"?/i.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? null;
  const total = Number(res.headers.get("Content-Length") ?? 0);
  if (!res.body || !total || !onProgress) return { blob: await res.blob(), filename };

  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(received / total);
  }
  return { blob: new Blob(chunks as BlobPart[]), filename };
}

export interface DownloadOptions {
  /** Source file name to export on its own; omit for every source together. */
  source?: string | null;
  /** GeoPackage only: one layer per source file in a single file. */
  split?: boolean;
  /** Export each parcel cut to the part inside the area of interest. */
  clipped?: boolean;
  /** Export the village boundaries layer instead of the parcels. */
  villages?: boolean;
}

export async function downloadResult(
  resultId: string,
  format: ExportFormat,
  options: DownloadOptions = {},
  onProgress?: ProgressCallback
) {
  const params = new URLSearchParams({ format });
  if (options.source) params.set("source", options.source);
  if (options.split) params.set("split", "true");
  if (options.clipped) params.set("clipped", "true");
  if (options.villages) params.set("layer", "villages");
  const { blob, filename } = await fetchBlob(`${BASE}/api/results/${resultId}/download?${params}`, onProgress);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename ?? `cadastral_parcels_${resultId.slice(0, 8)}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
