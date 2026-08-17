"""Preliminary FEMA NFHL flood screening with TWDB BLE status context."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry

from sitesense.geo import acreage

FEMA_MAPSERVER_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
FEMA_AVAILABILITY_URL = f"{FEMA_MAPSERVER_URL}/0/query"
FEMA_ZONES_URL = f"{FEMA_MAPSERVER_URL}/28/query"
TWDB_BLE_URL = "https://gis1.twdb.texas.gov/server/rest/services/WSC-FSCA-FM/Texas_BLE_Status/MapServer/0/query"
FLOOD_PAGE_SIZE = 1000
SENTINEL = -9999


class FloodSourceError(RuntimeError):
    """An upstream FEMA/TWDB request or response could not be used."""


@dataclass(frozen=True)
class FloodZoneResult:
    geometry: BaseGeometry
    zone_classification: str | None
    source_discriminator: str
    acres_intersected: float
    parcel_percent: float
    annual_chance: str | None
    attributes: dict[str, Any]


@dataclass
class FloodResult:
    zones: list[FloodZoneResult]
    metrics: dict[str, Any]
    warnings: list[dict[str, Any]]
    source_url: str
    retrieved_at: datetime
    stage_timings: dict[str, float]
    available: bool = True


def _query_geometry(geometry: BaseGeometry) -> dict[str, Any]:
    if isinstance(geometry, Polygon):
        rings: list[list[tuple[float, float]]] = [list(geometry.exterior.coords)]
        rings.extend(list(interior.coords) for interior in geometry.interiors)
    elif isinstance(geometry, MultiPolygon):
        rings = []
        for polygon in geometry.geoms:
            rings.append(list(polygon.exterior.coords))
            rings.extend(list(interior.coords) for interior in polygon.interiors)
    else:
        raise FloodSourceError("FEMA parcel geometry must be a polygon or multipolygon.")
    return {"rings": rings}


def _rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise FloodSourceError(f"{label} response had an unexpected shape.")
    rows = payload["features"]
    if any(not isinstance(row, dict) or not isinstance(row.get("geometry"), dict) for row in rows):
        raise FloodSourceError(f"{label} response contained invalid features.")
    return cast(list[dict[str, Any]], rows)


def _query(url: str, geometry: BaseGeometry, client: httpx.Client, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(
            url,
            params={
                "f": "json",
                "where": "1=1",
                "geometry": json.dumps(_query_geometry(geometry), separators=(",", ":")),
                "geometryType": "esriGeometryPolygon",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(FLOOD_PAGE_SIZE),
            },
        )
        response.raise_for_status()
        page = _rows(response.json(), label)
        rows.extend(page)
        if len(page) < FLOOD_PAGE_SIZE:
            return rows
        offset += len(page)


def _request(url: str, geometry: BaseGeometry, client: httpx.Client, label: str) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return _query(url, geometry, client, label)
        except (httpx.HTTPError, FloodSourceError) as exc:
            last_error = exc
    raise FloodSourceError(f"{label} request failed: {last_error}") from last_error


def _attrs(feature: dict[str, Any]) -> dict[str, Any]:
    attrs = feature.get("attributes", feature.get("properties", {}))
    if not isinstance(attrs, dict):
        raise FloodSourceError("FEMA feature attributes were invalid.")
    return attrs


def _geometry(value: dict[str, Any]) -> BaseGeometry:
    if value.get("type"):
        return shape(value)
    rings = value.get("rings")
    if not isinstance(rings, list) or not rings:
        raise FloodSourceError("FEMA feature geometry was invalid.")
    return Polygon(rings[0], [ring for ring in rings[1:] if isinstance(ring, list)])


def _value(attrs: dict[str, Any], key: str) -> Any:
    return attrs.get(key, attrs.get(key.casefold()))


def annual_chance(zone: Any, subtype: Any) -> str | None:
    zone_text = "" if zone is None else str(zone)
    subtype_text = "" if subtype is None else str(subtype)
    if zone_text in {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}:
        return "1% annual chance (100-year)"
    if "0.2 PCT" in subtype_text.upper():
        return "0.2% annual chance (500-year)"
    if zone_text == "X":
        return "Minimal mapped hazard (outside 0.2% annual chance floodplain)"
    if zone_text == "D":
        return "Undetermined hazard"
    return None


def _nullable_number(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return None if number == SENTINEL else number


def run_flood(
    parcel_geometry: BaseGeometry,
    parcel_acres: float,
    client: httpx.Client | None = None,
) -> FloodResult:
    retrieved_at = datetime.now(UTC)
    http_client = client or httpx.Client(timeout=30.0)
    close_client = client is None
    try:
        started = time.perf_counter()
        availability_rows = _request(FEMA_AVAILABILITY_URL, parcel_geometry, http_client, "FEMA availability")
        if not availability_rows:
            return FloodResult(
                zones=[],
                metrics={"parcel_acres": parcel_acres, "preliminary_planning_only": True},
                warnings=[{
                    "code": "fema_firm_unavailable",
                    "message": "No digital FIRM coverage intersects the parcel; this is not evidence of no flood risk.",
                }],
                source_url=FEMA_MAPSERVER_URL,
                retrieved_at=retrieved_at,
                stage_timings={"availability_query": time.perf_counter() - started},
                available=False,
            )
        zone_rows = _request(FEMA_ZONES_URL, parcel_geometry, http_client, "FEMA flood zones")
        ble_warnings: list[dict[str, Any]] = []
        ble_rows: list[dict[str, Any]] = []
        try:
            ble_rows = _request(TWDB_BLE_URL, parcel_geometry, http_client, "TWDB BLE status")
        except FloodSourceError as exc:
            ble_warnings.append({"code": "twdb_ble_partial", "message": str(exc)})
        required = (
            "DFIRM_ID", "FLD_AR_ID", "STUDY_TYP", "FLD_ZONE", "ZONE_SUBTY",
            "SFHA_TF", "STATIC_BFE", "DEPTH", "VELOCITY", "V_DATUM", "LEN_UNIT", "SOURCE_CIT",
        )
        zones: list[FloodZoneResult] = []
        sfha_acres = 0.0
        floodway_acres = 0.0
        bfe_values: list[float] = []
        for feature in zone_rows:
            attrs = _attrs(feature)
            missing = [key for key in required if key not in attrs]
            if missing:
                raise FloodSourceError(f"FEMA flood-zone response missing fields: {missing}")
            geometry = _geometry(feature["geometry"]).intersection(parcel_geometry)
            if geometry.is_empty:
                continue
            zone = _value(attrs, "FLD_ZONE")
            subtype = _value(attrs, "ZONE_SUBTY")
            sfha = _value(attrs, "SFHA_TF")
            unit_acres = acreage(geometry)
            if str(sfha) == "T":
                sfha_acres += unit_acres
            if "FLOODWAY" in str(subtype or "").upper():
                floodway_acres += unit_acres
            bfe = _nullable_number(_value(attrs, "STATIC_BFE"))
            if bfe is not None:
                bfe_values.append(bfe)
            zones.append(FloodZoneResult(
                geometry=geometry,
                zone_classification=None if zone is None else str(zone),
                source_discriminator="FEMA NFHL",
                acres_intersected=unit_acres,
                parcel_percent=unit_acres / parcel_acres * 100 if parcel_acres else 0.0,
                annual_chance=annual_chance(zone, subtype),
                attributes={**attrs, "STATIC_BFE": bfe, "DEPTH": _nullable_number(_value(attrs, "DEPTH")), "VELOCITY": _nullable_number(_value(attrs, "VELOCITY"))},
            ))
        for feature in ble_rows:
            attrs = _attrs(feature)
            geometry = _geometry(feature["geometry"]).intersection(parcel_geometry)
            if geometry.is_empty:
                continue
            zones.append(FloodZoneResult(
                geometry=geometry,
                zone_classification=None if _value(attrs, "STATUS") is None else str(_value(attrs, "STATUS")),
                source_discriminator="TWDB BLE status",
                acres_intersected=acreage(geometry),
                parcel_percent=acreage(geometry) / parcel_acres * 100 if parcel_acres else 0.0,
                annual_chance=None,
                attributes=attrs,
            ))
        warnings = ble_warnings + [{
            "code": "twdb_ble_status_only",
            "message": "TWDB BLE provides study status context only; authoritative supplemental flood extents are unavailable.",
        }]
        timings = {
            "availability_query": 0.0,
            "fema_zone_query": 0.0,
            "twdb_ble_query": 0.0,
            "flood_total": time.perf_counter() - started,
        }
        return FloodResult(
            zones=zones,
            metrics={
                "parcel_acres": parcel_acres,
                "fema_zone_acres": sum(zone.acres_intersected for zone in zones if zone.source_discriminator == "FEMA NFHL"),
                "sfha_acres": sfha_acres,
                "floodway_acres": floodway_acres,
                "static_bfe_min": min(bfe_values) if bfe_values else None,
                "static_bfe_max": max(bfe_values) if bfe_values else None,
                "fema_zone_count": sum(zone.source_discriminator == "FEMA NFHL" for zone in zones),
                "twdb_ble_status_count": sum(zone.source_discriminator == "TWDB BLE status" for zone in zones),
                "preliminary_planning_only": True,
            },
            warnings=warnings,
            source_url=FEMA_MAPSERVER_URL,
            retrieved_at=retrieved_at,
            stage_timings=timings,
        )
    finally:
        if close_client:
            http_client.close()
