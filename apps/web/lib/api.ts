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

export type Project = {
  id: string;
  name: string;
  client_id: string | null;
  organization_id: string;
};

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Authorization: "Bearer dev-token", "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = null;
    }
    if (
      typeof detail === "object" &&
      detail !== null &&
      "detail" in detail &&
      typeof detail.detail === "object" &&
      detail.detail !== null &&
      "message" in detail.detail &&
      typeof detail.detail.message === "string"
    ) {
      const typedDetail = detail.detail as { code?: unknown; message: string };
      throw new ApiError(
        typedDetail.message,
        response.status,
        typeof typedDetail.code === "string" ? typedDetail.code : null,
      );
    }
    throw new ApiError(`API request failed (${response.status})`, response.status);
  }
  return response.json() as Promise<T>;
}

export function listProjects(): Promise<Project[]> {
  return request<Project[]>("/projects");
}

export function createProject(name: string): Promise<Project> {
  return request<Project>("/projects", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
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
