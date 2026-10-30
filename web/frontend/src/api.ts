import type { FeatureCollection } from "geojson";

// Relative on purpose: the Vite dev server proxies /api to the backend.
const BASE = "";

export type ProgressCallback = (fraction: number) => void;
export type BBox = [number, number, number, number];
/** "both": the parcels that intersect the area, each tagged fully_inside, shown as two layers. */
export type MatchMode = "intersects" | "within" | "crossing" | "both";
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
  /** The villages found that have no cadastral parcels in the database (a layer and a CSV of their own). */
  no_parcels?: { count: number; per_state?: Record<string, number>; bbox?: BBox | null; error?: string };
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
  /** How many of the parcels lie completely inside the area. */
  fully_inside: number;
  bbox: BBox | null;
  seconds: number;
  aoi: FeatureCollection;
  /** DXF alignment searches: which lines were used. */
  alignment: DxfAlignmentInfo | null;
  input_kind?: "linear" | "area" | "legacy";
  /** The layer priority that was applied (null when there was a single layer): highest first, and how many parcels of each lower layer were left out as already covered. */
  priority?: { order: string[]; dropped: Record<string, number>; drop_fraction: number } | null;
  /** DXF chainage: how it was applied to the parcels (error is set when it could not be). */
  chainage: ChainageMeta | null;
}

export interface DxfCrsOption {
  code: number;
  name: string;
  /** The state the drawing lands in with this CRS, or "defined in the file". */
  hint: string | null;
}

export interface DxfFieldInfo {
  name: string;
  non_empty: number;
  chainage_like: number;
}

export interface DxfUpload {
  token: string;
  filename: string;
  fields: DxfFieldInfo[];
  default_field: string | null;
  sample: { columns: string[]; rows: string[][] };
  geometry_counts: Record<string, number>;
  coordinate: [number, number] | null;
  crs_in_file: { name: string; code: number | null } | null;
  crs_options: DxfCrsOption[];
}

export interface DxfAlignmentInfo {
  lines_found: number;
  /** Area files only: how many line pieces there were before pieces whose ends meet were joined. */
  pieces?: number;
  /** Area files only: "parallel_pair" = the two edges of a corridor were found, "longest_lines" = the longest line(s) were taken. */
  method?: "parallel_pair" | "longest_lines";
  /** Area files, parallel pair: how far apart the two lines run (narrowest, typical, widest) in metres. */
  width_m?: number[] | null;
  /** Area files, parallel pair: gaps in an edge that were bridged. */
  bridged?: { count: number; metres: number } | null;
  /** Area files, parallel pair: lengths in metres of other stretches of parallel lines that were left out. */
  other_sections_m?: number[];
  lines_used: number;
  mode_used: "two" | "single";
  lengths_m: number[];
  ignored: number;
  ignored_longest_m: number | null;
  warning: string | null;
}

export interface DxfChainageInfo {
  count: number;
  unparsed: number;
  unparsed_examples: string[];
  min: number;
  max: number;
  first: string;
  last: string;
}

export interface ChainageMeta {
  /** "linear": from the start chainage of an alignment search (no labels). */
  kind?: "linear";
  start_m?: number;
  start_end?: "A" | "B";
  labels?: number;
  labels_on_alignment?: number;
  labels_far?: number;
  first?: string;
  last?: string;
  alignment_length_m?: number;
  parcels_with_chainage?: number;
  parcels_with_labels_inside?: number;
  error?: string;
}

/** The session ran out (or sign-in is required): go to the sign-in page, and come back here afterwards. */
export function signInAgain(): void {
  window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

/** Who is signed in; user is null when sign-in is switched off on the server. */
export async function getMe(): Promise<{ user: string | null; auth: boolean }> {
  const res = await fetch(`${BASE}/api/me`);
  if (!res.ok) throw new Error(`Could not read the signed-in user (${res.status})`);
  return res.json();
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/api/logout`, { method: "POST" });
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
  onUploadComplete?: () => void,
  extra?: Record<string, string>
): Promise<{ job_id: string }> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("mode", mode);
  form.append("states", states.join(","));
  form.append("strip", String(strip));
  Object.entries(extra ?? {}).forEach(([k, v]) => form.append(k, v));

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/query`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => onUploadComplete?.();
    xhr.onload = () => {
      if (xhr.status === 401) {
        signInAgain();
        return;
      }
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

/** Reads the chosen area file on the server and returns it as GeoJSON, so it can be shown and zoomed to before searching. */
export function previewArea(
  files: File[],
  onProgress?: ProgressCallback,
  onUploadComplete?: () => void,
): Promise<{ aoi: FeatureCollection; alignment: DxfAlignmentInfo | null }> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/preview`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => onUploadComplete?.();
    xhr.onload = () => {
      if (xhr.status === 401) {
        signInAgain();
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.response));
      } else {
        try {
          reject(new Error(JSON.parse(xhr.response).detail ?? `Could not read the file (${xhr.status})`));
        } catch {
          reject(new Error(`Could not read the file (${xhr.status})`));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Could not read the file (network error)"));
    xhr.send(form);
  });
}

/** Uploads a DXF once; later steps (preview, search) refer to it by the token in the answer. */
export function uploadDxf(
  file: File,
  role: "alignment" | "chainage",
  onProgress?: ProgressCallback,
  onUploadComplete?: () => void
): Promise<DxfUpload> {
  const form = new FormData();
  form.append("file", file);
  form.append("role", role);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/dxf/upload`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => onUploadComplete?.();
    xhr.onload = () => {
      if (xhr.status === 401) {
        signInAgain();
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.response));
      } else {
        try {
          reject(new Error(JSON.parse(xhr.response).detail ?? `Could not read the DXF (${xhr.status})`));
        } catch {
          reject(new Error(`Could not read the DXF (${xhr.status})`));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Could not upload the DXF (network error)"));
    xhr.send(form);
  });
}

/** The alignment lines (longest line(s) of the DXF) or the chainage labels, as GeoJSON in EPSG:4326, to draw on the map. */
export async function previewDxf(params: {
  token: string;
  role: "alignment" | "chainage";
  crs: string;
  mode?: string;
  field?: string;
}): Promise<{ features: FeatureCollection; info: DxfAlignmentInfo | DxfChainageInfo }> {
  const form = new FormData();
  form.append("token", params.token);
  form.append("role", params.role);
  form.append("crs", params.crs);
  if (params.mode) form.append("mode", params.mode);
  if (params.field) form.append("field", params.field);
  const res = await fetch(`${BASE}/api/dxf/preview`, { method: "POST", body: form });
  if (!res.ok) throw new Error(await errorMessage(res, "Could not read the DXF"));
  return res.json();
}

/** The chainage labels that lie on the searched alignment (points with their text). */
export async function fetchChainage(resultId: string): Promise<FeatureCollection> {
  const res = await fetch(`${BASE}/api/results/${resultId}/features?layer=chainage`);
  if (!res.ok) throw new Error(await errorMessage(res, "Could not load the chainage labels"));
  return res.json();
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
  callbacks: SearchCallbacks = {},
  /** Extra form fields: the DXF alignment / chainage choices. */
  extra?: Record<string, string>
): Promise<QueryResult> {
  const { job_id } = await startQuery(files, mode, states, strip, callbacks.onUploadProgress, callbacks.onUploadComplete, extra);
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

/** The matched villages that have no cadastral parcels in the database. */
export async function fetchVillagesNoParcels(resultId: string): Promise<FeatureCollection> {
  const res = await fetch(`${BASE}/api/results/${resultId}/features?layer=villages_no_parcels`);
  if (!res.ok) throw new Error(await errorMessage(res, "Could not load the villages without parcels"));
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
  /** Export the CSV of villages that have no parcels in the database (format csv). */
  villagesNoParcels?: boolean;
  /** CSV and parcel tables: the processed LAP columns, or the columns of the source file. */
  data?: "lap" | "source";
  /** Parcel table: one CSV with the parcels of every layer (one parcel per place, by layer priority) instead of one per source. */
  combined?: boolean;
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
  if (options.villagesNoParcels) params.set("layer", "villages_no_parcels");
  if (options.data && (format === "csv" || format === "table")) params.set("data", options.data);
  if (options.combined) params.set("combined", "true");
  const { blob, filename } = await fetchBlob(`${BASE}/api/results/${resultId}/download?${params}`, onProgress);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename ?? `cadastral_parcels_${resultId.slice(0, 8)}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}

// ------------------------------------------------------------------ alignment / right of way / area inputs

export type InputRole = "alignment" | "row" | "area";

export interface InputUpload {
  token: string;
  filename: string;
  /** "dxf" files carry no coordinate system, so one is chosen; other formats define their own. */
  kind: "dxf" | "gis";
  crs_options: DxfCrsOption[];
  crs_in_file: { name: string; code: number | null } | null;
}

export interface InputInfo {
  warning: string | null;
  /** alignment */
  length_m?: number;
  ends?: { A: [number, number]; B: [number, number] };
  pieces?: number;
  lines_found?: number;
  ignored?: number;
  /** right of way / area */
  kind?: "polygon" | "two_lines";
  count?: number;
  lengths_m?: number[];
}

/** Uploads the file(s) of an alignment, right of way or area once; the preview and the search use the token. */
export function uploadInput(files: File[], onProgress?: ProgressCallback, onUploadComplete?: () => void): Promise<InputUpload> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/input/upload`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => onUploadComplete?.();
    xhr.onload = () => {
      if (xhr.status === 401) {
        signInAgain();
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.response));
      } else {
        try {
          reject(new Error(JSON.parse(xhr.response).detail ?? `Could not read the file (${xhr.status})`));
        } catch {
          reject(new Error(`Could not read the file (${xhr.status})`));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Could not upload the file (network error)"));
    xhr.send(form);
  });
}

/** What the search would use from an uploaded file (for drawing on the map): the centreline, the right of way, or the area. */
export async function previewInput(params: { token: string; role: InputRole; crs: string }): Promise<{ features: FeatureCollection; info: InputInfo }> {
  const form = new FormData();
  form.append("token", params.token);
  form.append("role", params.role);
  form.append("crs", params.crs);
  const res = await fetch(`${BASE}/api/input/preview`, { method: "POST", body: form });
  if (!res.ok) throw new Error(await errorMessage(res, "Could not read the file"));
  return res.json();
}
/** The chainage marks along an uploaded alignment for a start chainage and step (metres), as points with CH and KM text. */
export async function fetchMarks(params: { token: string; crs: string; startChainage: string; startEnd: "A" | "B"; step: string }): Promise<FeatureCollection> {
  const form = new FormData();
  form.append("token", params.token);
  form.append("crs", params.crs);
  form.append("start_chainage", params.startChainage);
  form.append("start_end", params.startEnd);
  form.append("step", params.step);
  const res = await fetch(`${BASE}/api/input/marks`, { method: "POST", body: form });
  if (!res.ok) throw new Error(await errorMessage(res, "Could not work out the chainage"));
  return res.json();
}
/** One parcel layer (source file) available where the area lies. */
export interface SourceLayer {
  source: string;
  states: string[];
  /** Parcels of this layer in the area; null when the state's parcels are not loaded yet. */
  parcels: number | null;
}

/** The layers available in the area (or the right of way), so they can be put in order of priority before searching. */
export async function fetchSources(fields: Record<string, string>): Promise<{ sources: SourceLayer[] }> {
  const form = new FormData();
  Object.entries(fields).forEach(([k, v]) => form.append(k, v));
  const res = await fetch(`${BASE}/api/input/sources`, { method: "POST", body: form });
  if (!res.ok) throw new Error(await errorMessage(res, "Could not list the layers"));
  return res.json();
}