import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import {
  Map as MLMap,
  NavigationControl,
  Popup,
  ScaleControl,
  type GeoJSONSource,
  type MapGeoJSONFeature,
  type MapMouseEvent,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection } from "geojson";
import type { BBox } from "./api";
import { findBasemap } from "./basemaps";

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };
const BASEMAP_ID = "basemap";
const PARCEL_HOVER_COLOR = "#ffd54f";
const AOI_COLOR = "#ff1744";
const STRIP_COLOR = "#ffb300";
const CLIP_COLOR = "#d500f9";
const VILLAGE_COLOR = "#2e7d32";
const NOPARCEL_COLOR = "#ff6d00";
const CHAINAGE_COLOR = "#1565c0";
// Stacking, bottom to top: basemap, strip, village boundaries, [parcel layers], clipped overlap,
// area-of-interest outline. Parcel layers are always inserted just below the clipped overlap.
const PARCEL_ANCHOR = "clip-fill";

// Attribute columns worth hiding in the popup: the fixed ones are shown in the header.
const MAX_POPUP_FEATURES = 6;
const HOVER_INTERVAL_MS = 60;
const MAX_VALUE_LENGTH = 140;

/** One source file's parcels, drawn as its own map layer. */
export interface ParcelLayer {
  /** Short id safe for use in MapLibre layer ids (not the file name). */
  id: string;
  color: string;
  data: FeatureCollection;
  visible: boolean;
  opacity: number;
}

export interface MapViewHandle {
  fitBounds: (bbox: BBox) => void;
}

interface Props {
  basemapId: string | null;
  aoi: FeatureCollection | null;
  /** Ordered bottom (first) to top (last). */
  parcelLayers: ParcelLayer[];
  showAoi: boolean;
  aoiOpacity: number;
  /** The polygon between two uploaded lines. */
  strip: FeatureCollection | null;
  showStrip: boolean;
  stripOpacity: number;
  /** The part of each matched parcel that lies inside the area of interest. */
  clip: FeatureCollection | null;
  showClip: boolean;
  clipOpacity: number;
  /** Colours of the static overlays (parcel layers carry their own colour). */
  aoiColor: string;
  stripColor: string;
  clipColor: string;
  /** Village boundaries: a layer of their own, drawn under the parcels. */
  villages: FeatureCollection | null;
  showVillages: boolean;
  villageOpacity: number;
  villageColor: string;
  /** Villages with no parcels in the database: drawn over the village layer in a colour of their own. */
  villagesNoParcels: FeatureCollection | null;
  showNoParcels: boolean;
  noParcelsOpacity: number;
  noParcelsColor: string;
  /** Chainage labels (points with their text), drawn above the parcels. */
  chainage: FeatureCollection | null;
  showChainage: boolean;
  chainageOpacity: number;
  chainageColor: string;
}

const sourceId = (id: string) => `parcels-${id}`;
const fillId = (id: string) => `parcels-${id}-fill`;
const lineId = (id: string) => `parcels-${id}-line`;

// Pop-up of a parcel or a village: only the processed (LAP) fields, and a "More" button for the columns of the source file.
// Built with DOM nodes (textContent), never innerHTML: attribute values come from the database and must not be interpreted as markup.
const PARCEL_FIELDS: [string, string][] = [
  ["parcel_number", "Parcel ID"],
  ["part", "Part"],
  ["area_m2", "Total Area"],
  ["inside_m2", "Affected Area"],
  ["chainage_from", "Chainage From"],
  ["chainage_to", "Chainage To"],
  ["village_name", "Village Name"],
  ["taluka_name", "Taluka Name"],
  ["district_name", "District Name"],
  ["state_name", "State Name"],
];
const VILLAGE_FIELDS: [string, string][] = [
  ["village_name", "Village Name"],
  ["taluka_name", "Taluka Name"],
  ["district_name", "District Name"],
  ["state_name", "State Name"],
];
// the processed properties the search adds to every parcel / village; everything else is a column of the source file
const INTERNAL_KEYS = new Set([
  "parcel_id", "village_id", "state", "source", "area_m2", "inside_m2", "inside_pct", "fully_inside", "priority_rank",
  "parcel_number", "village_name", "taluka_name", "district_name", "state_name", "chainage_from", "chainage_to", "part", "part_count",
]);
const AREA_KEYS = new Set(["area_m2", "inside_m2"]);

function cell(text: string, style: Partial<CSSStyleDeclaration> = {}): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = text;
  Object.assign(td.style, style);
  return td;
}

function fieldTable(rows: [string, string][]): HTMLTableElement {
  const table = document.createElement("table");
  table.style.borderCollapse = "collapse";
  table.style.marginTop = "4px";
  rows.forEach(([k, v]) => {
    const row = document.createElement("tr");
    row.append(cell(k, { color: "#666", paddingRight: "10px", verticalAlign: "top", whiteSpace: "nowrap" }), cell(v, { wordBreak: "break-word" }));
    table.appendChild(row);
  });
  return table;
}

const clip = (v: unknown) => {
  const text = String(v);
  return text.length > MAX_VALUE_LENGTH ? `${text.slice(0, MAX_VALUE_LENGTH)}…` : text;
};

function buildPopupContent(features: MapGeoJSONFeature[]): HTMLElement {
  const root = document.createElement("div");
  root.style.maxHeight = "300px";
  root.style.overflowY = "auto";
  root.style.minWidth = "230px";

  features.slice(0, MAX_POPUP_FEATURES).forEach((feature, index) => {
    const props = feature.properties ?? {};
    const block = document.createElement("div");
    if (index > 0) {
      block.style.borderTop = "1px solid #ddd";
      block.style.marginTop = "8px";
      block.style.paddingTop = "8px";
    }
    const title = document.createElement("div");
    title.style.fontWeight = "bold";
    const layer = feature.layer?.id;
    if (layer === "chainage-circle") {
      title.textContent = `Chainage mark · ${props.text ?? ""}`;
      block.appendChild(title);
      root.appendChild(block);
      return;
    }
    const isVillage = layer === "noparcel-fill" || layer === "village-fill";
    title.textContent =
      layer === "noparcel-fill" ? "Village without parcels" : isVillage ? "Village" : layer === PARCEL_ANCHOR ? "Parcel, part inside the area" : "Parcel";
    block.appendChild(title);

    const present = (v: unknown) => v !== null && v !== undefined && v !== "";
    const rows: [string, string][] = (isVillage ? VILLAGE_FIELDS : PARCEL_FIELDS)
      .filter(([key]) => {
        if (key === "chainage_from" || key === "chainage_to" || key === "part") return present(props[key]);
        if (key === "inside_m2") return Number(props[key]) > 0; // a search along a line has no affected area
        return true;
      })
      .map(([key, label]) => {
        const v = props[key];
        if (!present(v)) return [label, "–"];
        return [label, AREA_KEYS.has(key) ? `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })} m²` : clip(v)];
      });
    block.appendChild(fieldTable(rows));

    // The columns of the source file, behind a button.
    const source: [string, string][] = Object.entries(props)
      .filter(([key, value]) => !INTERNAL_KEYS.has(key) && present(value))
      .map(([key, value]) => [key.startsWith("attr_") && INTERNAL_KEYS.has(key.slice(5)) ? key.slice(5) : key, clip(value)]);
    const more = document.createElement("button");
    more.type = "button";
    more.textContent = "More";
    more.className = "mini";
    more.style.marginTop = "6px";
    const extra = document.createElement("div");
    extra.style.display = "none";
    extra.style.marginTop = "6px";
    const head = document.createElement("div");
    head.style.cssText = "font-size:11px;color:#666";
    head.textContent = `Source file: ${props.source ?? ""}`;
    extra.append(head, source.length > 0 ? fieldTable(source) : Object.assign(document.createElement("div"), { textContent: "No other attributes." }));
    more.addEventListener("click", () => {
      const open = extra.style.display === "none";
      extra.style.display = open ? "block" : "none";
      more.textContent = open ? "Less" : "More";
    });
    block.append(more, extra);
    root.appendChild(block);
  });

  if (features.length > MAX_POPUP_FEATURES) {
    const more = document.createElement("div");
    more.style.marginTop = "6px";
    more.style.color = "#666";
    more.textContent = `…and ${features.length - MAX_POPUP_FEATURES} more overlapping parcels`;
    root.appendChild(more);
  }
  return root;
}
const MapView = forwardRef<MapViewHandle, Props>(function MapView(
  {
    basemapId,
    aoi,
    parcelLayers,
    showAoi,
    aoiOpacity,
    strip,
    showStrip,
    stripOpacity,
    clip,
    showClip,
    clipOpacity,
    aoiColor,
    stripColor,
    clipColor,
    villages,
    showVillages,
    villageOpacity,
    villageColor,
    villagesNoParcels,
    showNoParcels,
    noParcelsOpacity,
    noParcelsColor,
    chainage,
    showChainage,
    chainageOpacity,
    chainageColor,
  },
  ref
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const coordsRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const [ready, setReady] = useState(false);
  // Which parcel layers currently exist on the map, and the data object each
  // was last given (so setData only runs when the data really changed).
  const managedRef = useRef<Map<string, FeatureCollection>>(new Map());

  useImperativeHandle(ref, () => ({
    fitBounds: (bbox) => {
      mapRef.current?.fitBounds(
        [
          [bbox[0], bbox[1]],
          [bbox[2], bbox[3]],
        ],
        { padding: 60, maxZoom: 18, duration: 600 }
      );
    },
  }));

  // Map creation: runs once. Only the area-of-interest layers are static;
  // parcel layers are added and removed as results come and go.
  useEffect(() => {
    if (!containerRef.current) return;
    const map = new MLMap({
      container: containerRef.current,
      // glyphs: only needed for the chainage text labels (shown from zoom 12); the dots work without them
      style: { version: 8, sources: {}, layers: [], glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf" },
      center: [79, 22],
      zoom: 4,
    });
    mapRef.current = map;
    map.addControl(new NavigationControl(), "top-right");
    map.addControl(new ScaleControl({ unit: "metric" }), "bottom-right");

    map.on("load", () => {
      map.addSource("strip", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "strip-fill",
        type: "fill",
        source: "strip",
        paint: { "fill-color": STRIP_COLOR, "fill-opacity": 0.25 },
      });
      map.addLayer({
        id: "strip-line",
        type: "line",
        source: "strip",
        paint: { "line-color": STRIP_COLOR, "line-width": 2, "line-dasharray": [3, 2] },
      });

      map.addSource("villages", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "village-fill",
        type: "fill",
        source: "villages",
        paint: { "fill-color": VILLAGE_COLOR, "fill-opacity": 0.1 },
      });
      map.addLayer({
        id: "village-line",
        type: "line",
        source: "villages",
        paint: { "line-color": VILLAGE_COLOR, "line-width": 2.5 },
      });

      map.addSource("noparcel", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "noparcel-fill",
        type: "fill",
        source: "noparcel",
        paint: { "fill-color": NOPARCEL_COLOR, "fill-opacity": 0.4 },
      });
      map.addLayer({
        id: "noparcel-line",
        type: "line",
        source: "noparcel",
        paint: { "line-color": NOPARCEL_COLOR, "line-width": 3 },
      });

      map.addSource("clip", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: PARCEL_ANCHOR,
        type: "fill",
        source: "clip",
        paint: { "fill-color": CLIP_COLOR, "fill-opacity": 0.5 },
      });
      // each parcel's part inside the area has its own outline: a pale casing under a darker line keeps neighbouring parts apart
      map.addLayer({
        id: "clip-casing",
        type: "line",
        source: "clip",
        paint: { "line-color": "#ffffff", "line-width": 3.5, "line-opacity": 0.9 },
      });
      map.addLayer({
        id: "clip-line",
        type: "line",
        source: "clip",
        paint: { "line-color": CLIP_COLOR, "line-width": 1.5 },
      });

      map.addSource("chainage", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "chainage-circle",
        type: "circle",
        source: "chainage",
        filter: ["!", ["has", "end"]],
        paint: { "circle-color": CHAINAGE_COLOR, "circle-radius": 4, "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 },
      });
      map.addLayer({
        id: "chainage-label",
        type: "symbol",
        source: "chainage",
        minzoom: 12,
        filter: ["!", ["has", "end"]],
        layout: { "text-field": ["get", "text"], "text-font": ["Open Sans Regular"], "text-size": 11, "text-offset": [0, 0.9], "text-anchor": "top" },
        paint: { "text-color": CHAINAGE_COLOR, "text-halo-color": "#ffffff", "text-halo-width": 1.5 },
      });
      // The two ends of the alignment (A and B): big, white text on a dark outline and an orange dot, readable on any basemap, at every zoom.
      map.addLayer({
        id: "chainage-end-circle",
        type: "circle",
        source: "chainage",
        filter: ["has", "end"],
        paint: { "circle-color": "#ff6d00", "circle-radius": 9, "circle-stroke-color": "#0b1f4d", "circle-stroke-width": 3 },
      });
      map.addLayer({
        id: "chainage-end-label",
        type: "symbol",
        source: "chainage",
        filter: ["has", "end"],
        layout: {
          "text-field": ["get", "text"],
          "text-font": ["Open Sans Regular"],
          "text-size": 20,
          "text-offset": [0, -1.0],
          "text-anchor": "bottom", // above the dot, so it does not sit on the chainage text of the start / end mark
          "text-allow-overlap": true,
          "text-ignore-placement": true,
        },
        paint: { "text-color": "#ffffff", "text-halo-color": "#0b1f4d", "text-halo-width": 3 },
      });

      map.addSource("aoi", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "aoi-fill",
        type: "fill",
        source: "aoi",
        filter: ["==", "$type", "Polygon"],
        paint: { "fill-color": AOI_COLOR, "fill-opacity": 0.12 },
      });
      map.addLayer({
        id: "aoi-line",
        type: "line",
        source: "aoi",
        filter: ["!=", "$type", "Point"],
        paint: { "line-color": AOI_COLOR, "line-width": 2.5 },
      });
      map.addLayer({
        id: "aoi-point",
        type: "circle",
        source: "aoi",
        filter: ["==", "$type", "Point"],
        paint: { "circle-color": AOI_COLOR, "circle-radius": 6, "circle-stroke-color": "#fff", "circle-stroke-width": 2 },
      });
      setReady(true);
    });

    const parcelFillLayers = () => [...managedRef.current.keys()].map(fillId).filter((id) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");

    // Hover highlight + click-for-attributes across every visible parcel layer.
    let hovered: { source: string; id: string | number } | null = null;
    const clearHover = () => {
      if (hovered) map.setFeatureState(hovered, { hover: false });
      hovered = null;
    };
    // queryRenderedFeatures over many overlapping polygons is costly, and the
    // browser can fire mousemove far more often than it is worth reacting to,
    // so the hover lookup runs at most every HOVER_INTERVAL_MS, on the latest
    // position. (A timer rather than requestAnimationFrame, which stops firing
    // whenever the page is not being painted.)
    let pendingMove: MapMouseEvent | null = null;
    let hoverTimer: number | null = null;
    const processMove = () => {
      hoverTimer = null;
      const e = pendingMove;
      pendingMove = null;
      if (!e) return;
      const layers = parcelFillLayers();
      const hit = layers.length ? map.queryRenderedFeatures(e.point, { layers })[0] : undefined;
      map.getCanvas().style.cursor = hit ? "pointer" : "";
      const next = hit && hit.id !== undefined ? { source: hit.source, id: hit.id } : null;
      if (next?.source === hovered?.source && next?.id === hovered?.id) return;
      clearHover();
      if (next) {
        map.setFeatureState(next, { hover: true });
        hovered = next;
      }
    };
    map.on("mousemove", (e) => {
      if (coordsRef.current) coordsRef.current.textContent = `${e.lngLat.lat.toFixed(6)}, ${e.lngLat.lng.toFixed(6)}`;
      pendingMove = e;
      if (hoverTimer === null) hoverTimer = window.setTimeout(processMove, HOVER_INTERVAL_MS);
    });
    map.on("mouseout", clearHover);
    map.on("click", (e) => {
      // Parcels on top, then the village they sit in (it is drawn underneath).
      const villageVisible = map.getLayer("village-fill") && map.getLayoutProperty("village-fill", "visibility") !== "none";
      const noParcelVisible = map.getLayer("noparcel-fill") && map.getLayoutProperty("noparcel-fill", "visibility") !== "none";
      const chainageVisible = map.getLayer("chainage-circle") && map.getLayoutProperty("chainage-circle", "visibility") !== "none";
      const clipVisible = map.getLayer(PARCEL_ANCHOR) && map.getLayoutProperty(PARCEL_ANCHOR, "visibility") !== "none";
      const layers = [
        ...(chainageVisible ? ["chainage-circle"] : []),
        ...(clipVisible ? [PARCEL_ANCHOR] : []),
        ...parcelFillLayers(),
        ...(noParcelVisible ? ["noparcel-fill"] : []),
        ...(villageVisible ? ["village-fill"] : []),
      ];
      if (layers.length === 0) return;
      const all = map.queryRenderedFeatures(e.point, { layers });
      // a parcel with a part inside the area is shown as that part (it carries the parcel's total area too), not twice
      const partOf = new Set(all.filter((h) => h.layer?.id === PARCEL_ANCHOR).map((h) => `${h.properties?.source}|${h.properties?.parcel_id}`));
      const hits = all.filter((h) => !h.layer?.id.startsWith("parcels-") || !partOf.has(`${h.properties?.source}|${h.properties?.parcel_id}`));
      if (hits.length === 0) return;
      new Popup({ maxWidth: "380px" }).setLngLat(e.lngLat).setDOMContent(buildPopupContent(hits)).addTo(map);
    });

    const managed = managedRef.current;
    return () => {
      map.remove();
      mapRef.current = null;
      managed.clear();
      setReady(false);
    };
  }, []);

  // Basemap: swapped in place and always kept at the very bottom.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (map.getLayer(BASEMAP_ID)) map.removeLayer(BASEMAP_ID);
    if (map.getSource(BASEMAP_ID)) map.removeSource(BASEMAP_ID);
    const basemap = findBasemap(basemapId);
    if (!basemap) return;
    map.addSource(BASEMAP_ID, {
      type: "raster",
      tiles: [basemap.tiles],
      tileSize: 256,
      maxzoom: 19,
      attribution: basemap.attribution,
    });
    const firstLayer = map.getStyle().layers.find((l) => l.id !== BASEMAP_ID)?.id;
    map.addLayer({ id: BASEMAP_ID, type: "raster", source: BASEMAP_ID }, firstLayer);
  }, [basemapId, ready]);

  // Parcel layers: add new ones, drop vanished ones, refresh data/paint/visibility,
  // and stack them in the given order, all below the area-of-interest outline.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const managed = managedRef.current;
    const wanted = new Set(parcelLayers.map((l) => l.id));

    for (const id of [...managed.keys()]) {
      if (wanted.has(id)) continue;
      [lineId(id), fillId(id)].forEach((layer) => map.getLayer(layer) && map.removeLayer(layer));
      if (map.getSource(sourceId(id))) map.removeSource(sourceId(id));
      managed.delete(id);
    }

    for (const layer of parcelLayers) {
      if (!managed.has(layer.id)) {
        map.addSource(sourceId(layer.id), { type: "geojson", data: layer.data, promoteId: "parcel_id" });
        map.addLayer(
          {
            id: fillId(layer.id),
            type: "fill",
            source: sourceId(layer.id),
            paint: {
              "fill-color": ["case", ["boolean", ["feature-state", "hover"], false], PARCEL_HOVER_COLOR, layer.color],
              "fill-opacity": layer.opacity,
            },
          },
          PARCEL_ANCHOR
        );
        map.addLayer(
          {
            id: lineId(layer.id),
            type: "line",
            source: sourceId(layer.id),
            paint: { "line-color": layer.color, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.2, 16, 1.2] },
          },
          PARCEL_ANCHOR
        );
        managed.set(layer.id, layer.data);
      } else if (managed.get(layer.id) !== layer.data) {
        (map.getSource(sourceId(layer.id)) as GeoJSONSource).setData(layer.data);
        managed.set(layer.id, layer.data);
      }

      const visibility = layer.visible ? "visible" : "none";
      map.setLayoutProperty(fillId(layer.id), "visibility", visibility);
      map.setLayoutProperty(lineId(layer.id), "visibility", visibility);
      map.setPaintProperty(fillId(layer.id), "fill-opacity", layer.opacity);
      map.setPaintProperty(lineId(layer.id), "line-opacity", Math.min(1, layer.opacity + 0.4));
      map.setPaintProperty(lineId(layer.id), "line-color", layer.color);
      map.setPaintProperty(fillId(layer.id), "fill-color", [
        "case",
        ["boolean", ["feature-state", "hover"], false],
        PARCEL_HOVER_COLOR,
        layer.color,
      ]);
    }

    // Each move puts the layer pair just under the AOI outline, so after
    // walking the list bottom-to-top the last entry ends up on top.
    for (const layer of parcelLayers) {
      map.moveLayer(fillId(layer.id), PARCEL_ANCHOR);
      map.moveLayer(lineId(layer.id), PARCEL_ANCHOR);
    }
  }, [parcelLayers, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("aoi") as GeoJSONSource).setData(aoi ?? EMPTY);
  }, [aoi, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("strip") as GeoJSONSource).setData(strip ?? EMPTY);
  }, [strip, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("clip") as GeoJSONSource).setData(clip ?? EMPTY);
  }, [clip, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("villages") as GeoJSONSource).setData(villages ?? EMPTY);
  }, [villages, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    ["village-fill", "village-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showVillages ? "visible" : "none"));
    map.setPaintProperty("village-fill", "fill-opacity", villageOpacity * 0.1);
    map.setPaintProperty("village-line", "line-opacity", villageOpacity);
    map.setPaintProperty("village-fill", "fill-color", villageColor);
    map.setPaintProperty("village-line", "line-color", villageColor);
  }, [showVillages, villageOpacity, villageColor, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("noparcel") as GeoJSONSource).setData(villagesNoParcels ?? EMPTY);
  }, [villagesNoParcels, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    ["noparcel-fill", "noparcel-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showNoParcels ? "visible" : "none"));
    map.setPaintProperty("noparcel-fill", "fill-opacity", noParcelsOpacity * 0.4);
    map.setPaintProperty("noparcel-line", "line-opacity", noParcelsOpacity);
    map.setPaintProperty("noparcel-fill", "fill-color", noParcelsColor);
    map.setPaintProperty("noparcel-line", "line-color", noParcelsColor);
  }, [showNoParcels, noParcelsOpacity, noParcelsColor, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("chainage") as GeoJSONSource).setData(chainage ?? EMPTY);
  }, [chainage, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    ["chainage-circle", "chainage-label", "chainage-end-circle", "chainage-end-label"].forEach((id) => map.setLayoutProperty(id, "visibility", showChainage ? "visible" : "none"));
    map.setPaintProperty("chainage-circle", "circle-opacity", chainageOpacity);
    map.setPaintProperty("chainage-circle", "circle-stroke-opacity", chainageOpacity);
    map.setPaintProperty("chainage-label", "text-opacity", chainageOpacity);
    map.setPaintProperty("chainage-end-circle", "circle-opacity", chainageOpacity);
    map.setPaintProperty("chainage-end-circle", "circle-stroke-opacity", chainageOpacity);
    map.setPaintProperty("chainage-end-label", "text-opacity", chainageOpacity);
    map.setPaintProperty("chainage-circle", "circle-color", chainageColor);
    map.setPaintProperty("chainage-label", "text-color", chainageColor);
  }, [showChainage, chainageOpacity, chainageColor, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    ["strip-fill", "strip-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showStrip ? "visible" : "none"));
    map.setPaintProperty("strip-fill", "fill-opacity", stripOpacity * 0.5);
    map.setPaintProperty("strip-line", "line-opacity", stripOpacity);
  }, [showStrip, stripOpacity, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    [PARCEL_ANCHOR, "clip-casing", "clip-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showClip ? "visible" : "none"));
    map.setPaintProperty(PARCEL_ANCHOR, "fill-opacity", clipOpacity);
    map.setPaintProperty("clip-line", "line-opacity", Math.min(1, clipOpacity + 0.3));
  }, [showClip, clipOpacity, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    ["aoi-fill", "aoi-line", "aoi-point"].forEach((id) => map.setLayoutProperty(id, "visibility", showAoi ? "visible" : "none"));
  }, [showAoi, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setPaintProperty("aoi-fill", "fill-color", aoiColor);
    map.setPaintProperty("aoi-line", "line-color", aoiColor);
    map.setPaintProperty("aoi-point", "circle-color", aoiColor);
  }, [aoiColor, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setPaintProperty("strip-fill", "fill-color", stripColor);
    map.setPaintProperty("strip-line", "line-color", stripColor);
  }, [stripColor, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setPaintProperty(PARCEL_ANCHOR, "fill-color", clipColor);
    map.setPaintProperty("clip-line", "line-color", clipColor);
  }, [clipColor, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setPaintProperty("aoi-fill", "fill-opacity", aoiOpacity * 0.35);
    map.setPaintProperty("aoi-line", "line-opacity", aoiOpacity);
    map.setPaintProperty("aoi-point", "circle-opacity", aoiOpacity);
  }, [aoiOpacity, ready]);

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
      <div
        ref={coordsRef}
        style={{
          position: "absolute",
          left: 8,
          bottom: 8,
          background: "rgba(255,255,255,0.85)",
          padding: "2px 6px",
          fontSize: 11,
          fontFamily: "monospace",
          borderRadius: 3,
          pointerEvents: "none",
        }}
      >
        lat, lon
      </div>
    </div>
  );
});

export default MapView;
