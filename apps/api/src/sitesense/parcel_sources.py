from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid5

import httpx
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from sitesense.geo import acreage, project_for_length

SOURCE_NAMESPACE = UUID("5a5bda8f-6d5e-4a9c-a75e-d4fb2dba4a72")
SITUS_MATCH_MAX_DISTANCE_METERS = 5000
_SEARCH_CACHE: dict[str, tuple[float, list[NormalizedParcel]]] = {}


@dataclass(frozen=True)
class ParcelFieldMapping:
    parcel_id: str
    situs_address: tuple[str, ...]
    legal_description: tuple[str, ...]
    appraisal_acres: str
    owner: str
    situs_number: str
    situs_street: str
    situs_suffix: str


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
    situs_number="situs_num",
    situs_street="situs_street",
    situs_suffix="situs_street_sufix",
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
    distance_meters: float | None = None
    contains_point: bool = False
    situs_match: bool = False

    @property
    def computed_acres(self) -> float:
        return acreage(self.geometry)


def _cache_key(point: Point | None, buffer_meters: float, address: str | None) -> str:
    point_key = f"{point.x:.6f}:{point.y:.6f}" if point is not None else "address-only"
    return f"{point_key}:{buffer_meters:.1f}:{normalize_situs(address or '')}"


class ParcelSourceAdapter(Protocol):
    source: CountySource

    async def search(
        self,
        point: Point | None,
        buffer_meters: float = 1000,
        address: str | None = None,
    ) -> list[NormalizedParcel]:
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
        max_record_count: int = 2000,
    ) -> None:
        self.source = source
        self.client = client
        self.timeout = timeout
        self.max_record_count = max_record_count

    async def search(
        self,
        point: Point | None,
        buffer_meters: float = 1000,
        address: str | None = None,
    ) -> list[NormalizedParcel]:
        key = f"{self.source.county}:{_cache_key(point, buffer_meters, address)}"
        cached = _SEARCH_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        spatial_params: dict[str, str] | None = None
        if point is not None:
            query_geometry = point.buffer(buffer_meters / 111_320) if buffer_meters else point
            minx, miny, maxx, maxy = query_geometry.bounds
            geometry = f"{minx},{miny},{maxx},{maxy}" if buffer_meters else f"{point.x},{point.y}"
            spatial_params = {
                "f": "json",
                "where": "1=1",
                "geometry": geometry,
                "geometryType": "esriGeometryEnvelope" if buffer_meters else "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
            }
        query = parse_situs_query(address) if address else None
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    spatial = await self._query_pages(client, spatial_params) if spatial_params else []
                    attribute = (
                        await self._query_pages(
                            client,
                            self._attribute_params(query),
                        )
                        if query
                        else []
                    )
                    parcels = self._merge(
                        [self._normalize(feature, point) for feature in spatial],
                        [self._normalize(feature, point, situs_match=True) for feature in attribute],
                    )
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

    async def _query_pages(
        self,
        client: httpx.AsyncClient,
        params: dict[str, str],
    ) -> list[dict[str, Any]]:
        page_size = self.max_record_count
        offset = 0
        features: list[dict[str, Any]] = []
        while True:
            page_params = {**params, "resultOffset": str(offset), "resultRecordCount": str(page_size)}
            response = await client.get(f"{self.source.url}/query", params=page_params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(payload["error"].get("message", "ArcGIS query failed"))
            page = payload.get("features", [])
            features.extend(page)
            if not payload.get("exceededTransferLimit"):
                break
            offset += len(page)
            if not page:
                break
        return features

    def _attribute_params(self, query: SitusQuery) -> dict[str, str]:
        street_clauses = [
            "("
            f"{self.source.fields.situs_street} LIKE '%{_escape_sql(token)}%' "
            f"OR {self.source.fields.situs_suffix} LIKE '%{_escape_sql(token)}%'"
            ")"
            for token in query.street_tokens
        ]
        where = (
            f"{self.source.fields.situs_number} = '{_escape_sql(query.house_number)}' "
            f"AND ({' AND '.join(street_clauses)})"
        )
        return {
            "f": "json",
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
        }

    def _merge(
        self,
        spatial: list[NormalizedParcel],
        attribute: list[NormalizedParcel],
    ) -> list[NormalizedParcel]:
        merged = {parcel.source_feature_id: parcel for parcel in spatial}
        for parcel in attribute:
            current = merged.get(parcel.source_feature_id)
            if current is None:
                merged[parcel.source_feature_id] = parcel
            else:
                current.situs_match = current.situs_match or parcel.situs_match
        return list(merged.values())

    def _normalize(
        self,
        feature: dict[str, Any],
        point: Point | None,
        situs_match: bool = False,
    ) -> NormalizedParcel:
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
        distance_meters = (
            project_for_length(point).distance(project_for_length(geometry))
            if point is not None
            else None
        )
        situs_match = situs_match and (
            distance_meters is None or distance_meters <= SITUS_MATCH_MAX_DISTANCE_METERS
        )
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
            distance_meters=distance_meters,
            contains_point=geometry.covers(point) if point is not None else False,
            situs_match=situs_match,
        )


async def search_counties(
    point: Point | None,
    county: str | None = None,
    buffer_meters: float = 1000,
    address: str | None = None,
    adapters: tuple[ParcelSourceAdapter, ...] | None = None,
    health: dict[str, str] | None = None,
    limit: int = 10,
) -> list[NormalizedParcel]:
    supported_county = next(
        (
            source.county
            for source in COUNTY_SOURCES
            if county is not None and source.county.casefold() == county.casefold()
        ),
        None,
    )
    selected = tuple(
        source for source in COUNTY_SOURCES
        if supported_county is None or source.county == supported_county
    )
    active = adapters or tuple(ArcGISParcelAdapter(source) for source in selected)
    if not active:
        return []
    searches = (
        (adapter.search(point, buffer_meters, address) if address else adapter.search(point, buffer_meters))
        for adapter in active
    )
    results = await asyncio.gather(*searches, return_exceptions=True)
    parcels: list[NormalizedParcel] = []
    for adapter, result in zip(active, results, strict=True):
        if isinstance(result, list):
            parcels.extend(result)
            if health is not None:
                health[adapter.source.county] = "healthy"
        elif health is not None:
            health[adapter.source.county] = f"unavailable:{type(result).__name__}"
    unique: dict[tuple[str, str], NormalizedParcel] = {}
    for parcel in parcels:
        key = (parcel.source_url, parcel.source_feature_id)
        existing = unique.get(key)
        if existing is None or (
            parcel.situs_match and not existing.situs_match
        ):
            unique[key] = parcel
    return sorted(
        unique.values(),
        key=lambda parcel: (
            not parcel.situs_match,
            not parcel.contains_point,
            parcel.distance_meters is None,
            parcel.distance_meters or 0,
        ),
    )[: max(0, limit)]


def source_for_url(url: str) -> CountySource | None:
    return next((source for source in COUNTY_SOURCES if source.data_source_url == url), None)


_ABBREVIATIONS = {
    "ST": "STREET",
    "RD": "ROAD",
    "LN": "LANE",
    "DR": "DRIVE",
    "HWY": "HIGHWAY",
    "CR": "COUNTY ROAD",
    "ESMT": "EASEMENT",
}
_DIRECTION_WORDS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
_STREET_SUFFIXES = {"STREET", "ROAD", "LANE", "DRIVE", "HIGHWAY", "EASEMENT", "COUNTY"}


def normalize_situs(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", " ", value.upper())
    tokens: list[str] = []
    for token in value.split():
        tokens.extend(_ABBREVIATIONS.get(token, token).split())
    return " ".join(tokens)


@dataclass(frozen=True)
class SitusQuery:
    house_number: str
    street_tokens: tuple[str, ...]


def parse_situs_query(address: str) -> SitusQuery | None:
    first_segment = address.split(",", 1)[0]
    match = re.match(r"\s*(\d+)\s+(.+)", first_segment)
    if not match:
        return None
    normalized = normalize_situs(match.group(2))
    tokens = tuple(
        token
        for token in normalized.split()
        if token not in _DIRECTION_WORDS and token not in _STREET_SUFFIXES
    )
    return SitusQuery(match.group(1), tokens or tuple(normalized.split()))


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")
