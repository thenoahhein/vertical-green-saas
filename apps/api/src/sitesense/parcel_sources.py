from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid5

import httpx
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from sitesense.geo import acreage, project_for_length

SOURCE_NAMESPACE = UUID("5a5bda8f-6d5e-4a9c-a75e-d4fb2dba4a72")
_SEARCH_CACHE: dict[str, tuple[float, list[NormalizedParcel]]] = {}


@dataclass(frozen=True)
class ParcelFieldMapping:
    parcel_id: str
    situs_address: tuple[str, ...]
    legal_description: tuple[str, ...]
    appraisal_acres: str
    owner: str


@dataclass(frozen=True)
class CountySource:
    county: str
    url: str
    data_source_url: str
    fields: ParcelFieldMapping


COMMON_FIELDS = ParcelFieldMapping(
    parcel_id="prop_id_text",
    situs_address=(
        "situs_num",
        "situs_street_prefx",
        "situs_street",
        "situs_street_sufix",
        "situs_city",
        "situs_state",
        "situs_zip",
    ),
    legal_description=("legal_desc", "legal_desc2", "legal_desc3"),
    appraisal_acres="legal_acreage",
    owner="file_as_name",
)

COUNTY_SOURCES = (
    CountySource(
        "Bastrop",
        "https://services.arcgis.com/aS4XD9PgZha28y8P/arcgis/rest/services/BastropCADWebService/FeatureServer/0",
        "https://services.arcgis.com/aS4XD9PgZha28y8P/arcgis/rest/services/BastropCADWebService/FeatureServer",
        COMMON_FIELDS,
    ),
    CountySource(
        "Lee",
        "https://services1.arcgis.com/la5KbvGUYLup9Aee/arcgis/rest/services/LeeCADWebService/FeatureServer/0",
        "https://services1.arcgis.com/la5KbvGUYLup9Aee/arcgis/rest/services/LeeCADWebService/FeatureServer",
        COMMON_FIELDS,
    ),
    CountySource(
        "Fayette",
        "https://services7.arcgis.com/INOomfRKQGxc9OW4/arcgis/rest/services/FayetteCADWebService/FeatureServer/0",
        "https://services7.arcgis.com/INOomfRKQGxc9OW4/arcgis/rest/services/FayetteCADWebService/FeatureServer",
        COMMON_FIELDS,
    ),
    CountySource(
        "Caldwell",
        "https://services.arcgis.com/rVxY74DxxIDrDbc0/arcgis/rest/services/CaldwellCADWebService/FeatureServer/0",
        "https://services.arcgis.com/rVxY74DxxIDrDbc0/arcgis/rest/services/CaldwellCADWebService/FeatureServer",
        ParcelFieldMapping(
            **{**COMMON_FIELDS.__dict__, "owner": "file_as_name"}
        ),
    ),
)


@dataclass
class NormalizedParcel:
    candidate_id: UUID
    county: str
    source_url: str
    source_feature_id: str
    parcel_id: str
    situs_address: str | None
    legal_description: str | None
    appraisal_acres: float | None
    owner: str | None
    geometry: BaseGeometry
    raw_attributes: dict[str, Any]
    distance_meters: float = 0.0
    contains_point: bool = False

    @property
    def computed_acres(self) -> float:
        return acreage(self.geometry)


def _cache_key(point: Point, buffer_meters: float) -> str:
    return f"{point.x:.6f}:{point.y:.6f}:{buffer_meters:.1f}"


class ParcelSourceAdapter(Protocol):
    source: CountySource

    async def search(self, point: Point, buffer_meters: float = 0) -> list[NormalizedParcel]:
        ...


def _arcgis_geometry(value: dict[str, Any]) -> BaseGeometry:
    rings = value.get("rings", [])
    if not rings:
        raise ValueError("ArcGIS feature has no polygon rings")
    geometry = shape({"type": "Polygon", "coordinates": rings})
    if isinstance(geometry, Polygon):
        return MultiPolygon([geometry])
    if isinstance(geometry, MultiPolygon):
        return geometry
    raise ValueError("ArcGIS feature geometry is not a polygon")


def _text(attributes: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    values = [str(attributes[field]).strip() for field in fields if attributes.get(field) not in (None, "")]
    return " ".join(values) or None


class ArcGISParcelAdapter:
    def __init__(
        self,
        source: CountySource,
        client: httpx.AsyncClient | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.source = source
        self.client = client
        self.timeout = timeout

    async def search(self, point: Point, buffer_meters: float = 0) -> list[NormalizedParcel]:
        # ArcGIS accepts WGS84 point queries. A small envelope around the point
        # catches parcels straddling service/county boundaries without broad scans.
        query_geometry = point.buffer(buffer_meters / 111_320) if buffer_meters else point
        minx, miny, maxx, maxy = query_geometry.bounds
        geometry = f"{minx},{miny},{maxx},{maxy}" if buffer_meters else f"{point.x},{point.y}"
        key = f"{self.source.county}:{_cache_key(point, buffer_meters)}"
        cached = _SEARCH_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        params = {
            "f": "json",
            "where": "1=1",
            "geometry": geometry,
            "geometryType": "esriGeometryEnvelope" if buffer_meters else "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultRecordCount": 10,
        }
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await client.get(f"{self.source.url}/query", params=params)
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("error"):
                        raise RuntimeError(payload["error"].get("message", "ArcGIS query failed"))
                    parcels = [self._normalize(feature, point) for feature in payload.get("features", [])]
                    _SEARCH_CACHE[key] = (time.monotonic() + 300, parcels)
                    return parcels
                except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                    last_error = exc
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.05 * (attempt + 1))
            raise RuntimeError("ArcGIS query failed") from last_error
        finally:
            if own_client:
                await client.aclose()

    def _normalize(self, feature: dict[str, Any], point: Point) -> NormalizedParcel:
        attributes = feature.get("attributes") or {}
        geometry = _arcgis_geometry(feature.get("geometry") or {})
        mapping = self.source.fields
        source_feature_id = str(attributes.get("ObjectID_1") or attributes.get("OBJECTID") or attributes.get(mapping.parcel_id))
        parcel_id = str(attributes.get(mapping.parcel_id) or source_feature_id)
        try:
            appraisal = float(attributes[mapping.appraisal_acres]) if attributes.get(mapping.appraisal_acres) is not None else None
        except (TypeError, ValueError):
            appraisal = None
        candidate_id = uuid5(SOURCE_NAMESPACE, f"{self.source.county}:{source_feature_id}")
        return NormalizedParcel(
            candidate_id=candidate_id,
            county=self.source.county,
            source_url=self.source.data_source_url,
            source_feature_id=source_feature_id,
            parcel_id=parcel_id,
            situs_address=_text(attributes, mapping.situs_address),
            legal_description=_text(attributes, mapping.legal_description),
            appraisal_acres=appraisal,
            owner=_text(attributes, (mapping.owner,)),
            geometry=geometry,
            raw_attributes=attributes,
            distance_meters=project_for_length(point).distance(project_for_length(geometry).centroid),
            contains_point=geometry.covers(point),
        )


async def search_counties(
    point: Point,
    county: str | None = None,
    buffer_meters: float = 250,
    adapters: tuple[ParcelSourceAdapter, ...] | None = None,
    health: dict[str, str] | None = None,
) -> list[NormalizedParcel]:
    selected = tuple(
        source for source in COUNTY_SOURCES
        if county is None or source.county.casefold() == county.casefold()
    )
    active = adapters or tuple(ArcGISParcelAdapter(source) for source in selected)
    if not active:
        return []
    results = await asyncio.gather(
        *(adapter.search(point, buffer_meters) for adapter in active),
        return_exceptions=True,
    )
    parcels: list[NormalizedParcel] = []
    for adapter, result in zip(active, results, strict=True):
        if isinstance(result, list):
            parcels.extend(result)
            if health is not None:
                health[adapter.source.county] = "healthy"
        elif health is not None:
            health[adapter.source.county] = f"unavailable:{type(result).__name__}"
    return sorted(parcels, key=lambda parcel: (not parcel.contains_point, parcel.distance_meters))


def source_for_url(url: str) -> CountySource | None:
    return next((source for source in COUNTY_SOURCES if source.data_source_url == url), None)
