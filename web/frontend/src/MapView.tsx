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
// Stacking, bottom to top: basemap, strip, village boundaries, [parcel layers], clipped overlap,
// area-of-interest outline. Parcel layers are always inserted just below the clipped overlap.
const PARCEL_ANCHOR = "clip-fill";

// Attribute columns worth hiding in the popup: the fixed ones are shown in the header.
const HEADER_KEYS = new Set(["parcel_id", "state", "source"]);
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
}

const sourceId = (id: string) => `parcels-${id}`;
const fillId = (id: string) => `parcels-${id}-fill`;
const lineId = (id: string) => `parcels-${id}-line`;

// Built with DOM nodes (textContent), never innerHTML: attribute values come
// from the database and must not be interpreted as markup.
function buildPopupContent(features: MapGeoJSONFeature[]): HTMLElement {
  const root = document.createElement("div");
  root.style.maxHeight = "280px";
  root.style.overflowY = "auto";
  root.style.minWidth = "220px";

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
    title.textContent =
      feature.layer?.id === "village-fill"
        ? `Village boundary · ${props.village ?? "unnamed"} (${props.state ?? ""})`
        : `${props.state ?? ""} · ${props.source ?? ""}`;
    block.appendChild(title);

    const table = document.createElement("table");
    table.style.borderCollapse = "collapse";
    table.style.marginTop = "4px";
    Object.entries(props)
      .filter(([key, value]) => !HEADER_KEYS.has(key) && value !== null && value !== "")
      .forEach(([key, value]) => {
        const row = document.createElement("tr");
        const k = document.createElement("td");
        k.textContent = key;
        k.style.color = "#666";
        k.style.paddingRight = "8px";
        k.style.verticalAlign = "top";
        const v = document.createElement("td");
        const text = String(value);
        v.textContent = text.length > MAX_VALUE_LENGTH ? `${text.slice(0, MAX_VALUE_LENGTH)}…` : text;
        v.style.wordBreak = "break-all";
        row.append(k, v);
        table.appendChild(row);
      });
    block.appendChild(table);
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
      style: { version: 8, sources: {}, layers: [] },
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

      map.addSource("clip", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: PARCEL_ANCHOR,
        type: "fill",
        source: "clip",
        paint: { "fill-color": CLIP_COLOR, "fill-opacity": 0.5 },
      });
      map.addLayer({
        id: "clip-line",
        type: "line",
        source: "clip",
        paint: { "line-color": CLIP_COLOR, "line-width": 1 },
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
      const layers = [...parcelFillLayers(), ...(villageVisible ? ["village-fill"] : [])];
      if (layers.length === 0) return;
      const hits = map.queryRenderedFeatures(e.point, { layers });
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
    ["strip-fill", "strip-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showStrip ? "visible" : "none"));
    map.setPaintProperty("strip-fill", "fill-opacity", stripOpacity * 0.5);
    map.setPaintProperty("strip-line", "line-opacity", stripOpacity);
  }, [showStrip, stripOpacity, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    [PARCEL_ANCHOR, "clip-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showClip ? "visible" : "none"));
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
