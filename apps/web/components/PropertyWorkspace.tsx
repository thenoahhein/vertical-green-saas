"use client";

import maplibregl, { type GeoJSONSource, type Map as MapInstance } from "maplibre-gl";
import { useEffect, useRef, useState } from "react";

import { confirmParcel, searchParcels, type ConfirmedParcel, type ParcelCandidate } from "../lib/api";
import { layerRegistry } from "../lib/layers";

const PROJECT_ID = process.env.NEXT_PUBLIC_PROJECT_ID ?? "00000000-0000-0000-0000-000000000000";
const BASEMAP_STYLE = process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/liberty";

function feature(candidate: ParcelCandidate | ConfirmedParcel): GeoJSON.Feature<GeoJSON.Geometry> {
  return { type: "Feature", properties: {}, geometry: candidate.geometry as GeoJSON.Polygon | GeoJSON.MultiPolygon };
}

function outerRing(candidate: ParcelCandidate): number[][] {
  const coordinates = candidate.geometry.coordinates;
  return candidate.geometry.type === "Polygon"
    ? (coordinates[0] as number[][])
    : ((coordinates[0] as number[][][])[0] as number[][]);
}

export default function PropertyWorkspace() {
  const mapElement = useRef<HTMLDivElement>(null);
  const map = useRef<MapInstance | null>(null);
  const [address, setAddress] = useState("");
  const [candidates, setCandidates] = useState<ParcelCandidate[]>([]);
  const [selected, setSelected] = useState<ParcelCandidate | null>(null);
  const [confirmed, setConfirmed] = useState<ConfirmedParcel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [layers, setLayers] = useState(layerRegistry);

  useEffect(() => {
    if (!mapElement.current) return;
    const instance = new maplibregl.Map({ container: mapElement.current, style: BASEMAP_STYLE, center: [-97.3, 30.1], zoom: 9 });
    instance.addControl(new maplibregl.NavigationControl(), "top-right");
    instance.on("load", () => {
      instance.addSource("parcel-selection", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      instance.addLayer({ id: "parcel-fill", type: "fill", source: "parcel-selection", paint: { "fill-color": "#3b82f6", "fill-opacity": 0.24 } });
      instance.addLayer({ id: "parcel-outline", type: "line", source: "parcel-selection", paint: { "line-color": "#1d4ed8", "line-width": 2 } });
    });
    instance.on("click", (event) => void runSearch(event.lngLat.lng, event.lngLat.lat));
    map.current = instance;
    return () => instance.remove();
  }, []);

  useEffect(() => {
    const source = map.current?.getSource("parcel-selection") as GeoJSONSource | undefined;
    if (source) source.setData(selected ? feature(selected) : confirmed ? feature(confirmed) : { type: "FeatureCollection", features: [] });
  }, [selected, confirmed]);

  async function runSearch(longitude?: number, latitude?: number) {
    setError(null);
    try {
      const response = await searchParcels(longitude !== undefined && latitude !== undefined ? { longitude, latitude } : { address });
      setCandidates(response.candidates);
      setSelected(response.candidates[0] ?? null);
      map.current?.flyTo({ center: [response.longitude, response.latitude], zoom: 15 });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Parcel search failed");
    }
  }

  function selectCandidate(candidate: ParcelCandidate) {
    setSelected(candidate);
    const bounds = new maplibregl.LngLatBounds();
    outerRing(candidate).forEach((coordinate) => bounds.extend([coordinate[0], coordinate[1]]));
    map.current?.fitBounds(bounds, { padding: 80, maxZoom: 17 });
  }

  async function confirm() {
    if (!selected) return;
    try {
      setConfirmed(await confirmParcel(PROJECT_ID, selected));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Parcel confirmation failed");
    }
  }

  return (
    <main className="workspace">
      <aside className="panel">
        <h1>SiteSense property workspace</h1>
        <form onSubmit={(event) => { event.preventDefault(); void runSearch(); }}>
          <input value={address} onChange={(event) => setAddress(event.target.value)} placeholder="Search a Central Texas address" />
          <button type="submit">Search parcels</button>
        </form>
        {error && <p className="error">{error}</p>}
        <p className="hint">Click anywhere on the map for a manual coordinate fallback.</p>
        <h2>Parcel candidates</h2>
        <ul className="candidates">
          {candidates.map((candidate) => (
            <li key={candidate.candidate_id} onMouseEnter={() => setSelected(candidate)} onClick={() => selectCandidate(candidate)}>
              <strong>{candidate.situs_address ?? candidate.parcel_id}</strong>
              <span>{candidate.county} · {candidate.computed_acres.toFixed(2)} computed acres</span>
              <span>{candidate.appraisal_acres === null ? "Appraisal acreage unavailable" : `${candidate.appraisal_acres.toFixed(2)} appraisal acres`}</span>
            </li>
          ))}
        </ul>
        {selected && <button type="button" onClick={() => void confirm()}>Confirm selected parcel</button>}
        {confirmed && <section className="confirmed"><h2>Confirmed parcel</h2><p>{confirmed.computed_acres?.toFixed(2)} computed acres · {confirmed.appraisal_record_acres?.toFixed(2) ?? "—"} appraisal acres</p><p>{confirmed.disclaimer}</p></section>}
        <h2>Layers</h2>
        {layers.map((layer) => <label key={layer.id}><input type="checkbox" checked={layer.enabled} onChange={() => setLayers((current) => current.map((item) => item.id === layer.id ? { ...item, enabled: !item.enabled } : item))} /> {layer.label}</label>)}
      </aside>
      <div ref={mapElement} className="map" aria-label="Interactive parcel map" />
    </main>
  );
}
