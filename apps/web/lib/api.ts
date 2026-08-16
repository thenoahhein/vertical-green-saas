export type GeoJsonGeometry = {
  type: "Polygon" | "MultiPolygon";
  coordinates: number[][][] | number[][][][];
};

export type ParcelCandidate = {
  candidate_id: string;
  county: string;
  source_url: string;
  source_feature_id: string;
  parcel_id: string;
  situs_address: string | null;
  legal_description: string | null;
  appraisal_acres: number | null;
  computed_acres: number;
  owner: string | null;
  geometry: GeoJsonGeometry;
  raw_attributes: Record<string, unknown>;
  distance_meters: number | null;
  contains_point: boolean;
};

export type ParcelSearchResponse = {
  candidates: ParcelCandidate[];
  latitude: number;
  longitude: number;
  matched_address: string | null;
  geocoder_failed: boolean;
  disclaimer: string;
};

export type ConfirmedParcel = {
  parcel_id: string;
  project_id: string;
  county: string;
  appraisal_parcel_id: string;
  situs_address: string | null;
  legal_description: string | null;
  appraisal_record_acres: number | null;
  computed_acres: number | null;
  geometry: GeoJsonGeometry;
  disclaimer: string;
};

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Authorization: "Bearer dev-token", "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new Error(`API request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function searchParcels(query: { address?: string; latitude?: number; longitude?: number }): Promise<ParcelSearchResponse> {
  const params = new URLSearchParams();
  if (query.address) params.set("address", query.address);
  if (query.latitude !== undefined) params.set("latitude", String(query.latitude));
  if (query.longitude !== undefined) params.set("longitude", String(query.longitude));
  return request<ParcelSearchResponse>(`/parcel-search?${params.toString()}`);
}

export function confirmParcel(projectId: string, candidate: ParcelCandidate): Promise<ConfirmedParcel> {
  return request<ConfirmedParcel>(`/projects/${projectId}/parcel`, {
    method: "POST",
    body: JSON.stringify({ candidate }),
  });
}
