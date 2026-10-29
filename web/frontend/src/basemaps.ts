// Same free/no-key XYZ endpoints QGIS plugins like HCMGIS or Quick Map
// Services use -- fine for an internal engineering tool, but note the
// Google tile endpoints aren't an official public API (no SLA, ToS
// restricts them to non-commercial/internal use).
export interface Basemap {
  id: string;
  label: string;
  tiles: string;
  attribution: string;
}

export const BASEMAPS: Basemap[] = [
  {
    id: "google-roadmap",
    label: "Google Roadmap",
    tiles: "https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}",
    attribution: "© Google",
  },
  {
    id: "osm",
    label: "OpenStreetMap",
    tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: "© OpenStreetMap contributors",
  },
  {
    id: "google-satellite",
    label: "Google Satellite",
    tiles: "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
    attribution: "© Google",
  },
  {
    id: "google-hybrid",
    label: "Google Hybrid",
    tiles: "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
    attribution: "© Google",
  },
];

export function findBasemap(id: string | null): Basemap | undefined {
  return BASEMAPS.find((b) => b.id === id);
}
