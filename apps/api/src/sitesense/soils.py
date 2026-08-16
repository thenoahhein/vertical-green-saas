"""Preliminary SSURGO soil-map-unit analysis for a confirmed parcel."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from shapely import wkt
from shapely.geometry.base import BaseGeometry

from sitesense.geo import acreage

SDA_URL = "https://SDMDataAccess.sc.egov.usda.gov/Tabular/post.rest"
SDA_DATASET = "USDA SSURGO Soil Data Access"
MAX_AOI_WKT_CHARS = 7000
MUKEY_PATTERN = re.compile(r"^\d+$")

QUERY_A = """~DeclareGeometry(@aoi)~
SELECT @aoi = geometry::STGeomFromText('{wkt}', 4326)
~DeclareIdGeomTable(@intersectedPolygonGeometries)~
~GetClippedMapunits(@aoi,polygon,geo,@intersectedPolygonGeometries)~
SELECT mukey, mapunit.musym, mapunit.muname,
       GEOGRAPHY::STGeomFromWKB(geom.STUnion(geom.STStartPoint()).STAsBinary(),4326).STArea()*0.000247105 AS acres,
       geom.STAsText() AS wkt
FROM @intersectedPolygonGeometries JOIN mapunit ON mapunit.mukey = id"""

QUERY_B = """SELECT mu.mukey, mu.musym, mu.muname, mu.farmlndcl,
       c.cokey, c.compname, c.comppct_r,
       c.slope_l, c.slope_r, c.slope_h,
       c.drainagecl, c.hydgrp, c.taxclname,
       muagg.flodfreqdcd,
       (SELECT TOP 1 cm.pondfreqcl FROM comonth cm WHERE cm.cokey = c.cokey AND cm.pondfreqcl IS NOT NULL) AS pondfreqcl,
       c.runoff, c.hydricrating,
       muagg.aws0150wta, muagg.brockdepmin, muagg.wtdepannmin,
       (SELECT TOP 1 ch.ksat_r FROM chorizon ch WHERE ch.cokey = c.cokey ORDER BY ch.hzdept_r) AS ksat_surface_r
FROM mapunit mu
JOIN component c ON c.mukey = mu.mukey
LEFT JOIN muaggatt muagg ON muagg.mukey = mu.mukey
WHERE mu.mukey IN ({mukeys})
ORDER BY mu.mukey, c.comppct_r DESC"""

SOIL_COLUMNS = (
    "mukey", "musym", "muname", "farmlndcl", "cokey", "compname", "comppct_r",
    "slope_l", "slope_r", "slope_h", "drainagecl", "hydgrp", "taxclname",
    "flodfreqdcd", "pondfreqcl", "runoff", "hydricrating", "aws0150wta",
    "brockdepmin", "wtdepannmin", "ksat_surface_r",
)


class SoilsSourceError(RuntimeError):
    """An upstream SDA request or response could not be used."""


@dataclass(frozen=True)
class SoilComponent:
    mukey: str
    cokey: str | None
    name: str | None
    percent: float | None
    slope_low: float | None
    slope_representative: float | None
    slope_high: float | None
    drainage_class: str | None
    hydrologic_group: str | None
    farmland_classification: str | None
    available_water_storage: float | None
    ksat: float | None
    depth_to_restrictive_layer: float | None
    flooding_frequency: str | None
    ponding_class: str | None


@dataclass(frozen=True)
class SoilUnitResult:
    geometry: BaseGeometry
    mukey: str
    musym: str | None
    map_unit_name: str | None
    acres: float
    parcel_percent: float
    reported_acres: float | None
    dominant_component: SoilComponent | None
    components: tuple[SoilComponent, ...]


@dataclass
class SoilsResult:
    units: list[SoilUnitResult]
    metrics: dict[str, Any]
    warnings: list[dict[str, Any]]
    source_url: str
    retrieved_at: datetime
    stage_timings: dict[str, float]


def _prepare_aoi(parcel_geometry: BaseGeometry) -> tuple[BaseGeometry, list[dict[str, Any]]]:
    """Bound SDA's AOI while recording any geometry transformation."""
    precise = wkt.loads(wkt.dumps(parcel_geometry, rounding_precision=6, trim=True))
    if len(precise.wkt) <= MAX_AOI_WKT_CHARS:
        return precise, []
    for tolerance in (1e-7, 5e-7, 1e-6, 5e-6):
        simplified = precise.simplify(tolerance, preserve_topology=True)
        if len(simplified.wkt) <= MAX_AOI_WKT_CHARS:
            return simplified, [{
                "code": "soils_aoi_simplified",
                "message": "SDA AOI coordinates were simplified to remain within the query size bound.",
                "aoi_geometry_method": f"simplified_{tolerance:g}_degrees",
            }]
    envelope = precise.envelope
    return envelope, [{
        "code": "soils_aoi_envelope_fallback",
        "message": "SDA AOI was reduced to its bounding envelope to remain within the query size bound.",
        "aoi_geometry_method": "envelope",
    }]


def _rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("Table"), list):
        raise SoilsSourceError(f"SDA {label} response had an unexpected shape.")
    rows = payload["Table"]
    if not rows:
        return []
    if all(isinstance(row, dict) for row in rows):
        return cast(list[dict[str, Any]], rows)
    if not isinstance(rows[0], list) or not all(isinstance(value, str) for value in rows[0]):
        raise SoilsSourceError(f"SDA {label} response contained an invalid column header row.")
    headers = rows[0]
    if any(not isinstance(row, list) or len(row) != len(headers) for row in rows[1:]):
        raise SoilsSourceError(f"SDA {label} response contained an invalid data row.")
    return [dict(zip(headers, row, strict=True)) for row in rows[1:]]


def _post(query: str, client: httpx.Client, label: str, attempts: int = 2) -> list[dict[str, Any]]:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.post(SDA_URL, json={"query": query, "format": "JSON+COLUMNNAME"})
            response.raise_for_status()
            return _rows(response.json(), label)
        except (httpx.HTTPError, ValueError, SoilsSourceError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.5)
    raise SoilsSourceError(f"SDA {label} request failed: {last}") from last


def _number(row: dict[str, Any], name: str) -> float | None:
    value = row[name]
    return None if value is None or value == "" else float(value)


def _component(row: dict[str, Any]) -> SoilComponent:
    missing = [name for name in SOIL_COLUMNS if name not in row]
    if missing:
        raise SoilsSourceError(f"SDA component response is missing columns: {missing}")
    return SoilComponent(
        mukey=str(row["mukey"]),
        cokey=None if row["cokey"] is None else str(row["cokey"]),
        name=None if row["compname"] is None else str(row["compname"]),
        percent=_number(row, "comppct_r"),
        slope_low=_number(row, "slope_l"),
        slope_representative=_number(row, "slope_r"),
        slope_high=_number(row, "slope_h"),
        drainage_class=None if row["drainagecl"] is None else str(row["drainagecl"]),
        hydrologic_group=None if row["hydgrp"] is None else str(row["hydgrp"]),
        farmland_classification=None if row["farmlndcl"] is None else str(row["farmlndcl"]),
        available_water_storage=_number(row, "aws0150wta"),
        ksat=_number(row, "ksat_surface_r"),
        depth_to_restrictive_layer=_number(row, "brockdepmin"),
        flooding_frequency=None if row["flodfreqdcd"] is None else str(row["flodfreqdcd"]),
        ponding_class=None if row["pondfreqcl"] is None else str(row["pondfreqcl"]),
    )


def run_soils(parcel_geometry: BaseGeometry, parcel_acres: float, client: httpx.Client | None = None) -> SoilsResult:
    """Fetch and reconcile clipped SSURGO map units for a parcel."""
    retrieved_at = datetime.now(UTC)
    timings: dict[str, float] = {}
    http_client = client or httpx.Client(timeout=20.0)
    close_client = client is None
    try:
        aoi_geometry, aoi_warnings = _prepare_aoi(parcel_geometry)
        aoi_acres = acreage(aoi_geometry)
        started = time.perf_counter()
        map_rows = _post(QUERY_A.format(wkt=aoi_geometry.wkt), http_client, "map-unit")
        timings["map_unit_query"] = time.perf_counter() - started
        if not map_rows:
            return SoilsResult(
                [],
                {
                    "parcel_acres": parcel_acres,
                    "aoi_acres": aoi_acres,
                    "coverage_fraction": 0.0,
                    "aoi_geometry_method": "native_wkt" if not aoi_warnings else aoi_warnings[-1]["aoi_geometry_method"],
                },
                aoi_warnings + [{
                    "code": "soils_source_unavailable",
                    "message": "SDA returned no map units for the parcel AOI.",
                }],
                SDA_URL, retrieved_at, timings,
            )
        required = {"mukey", "musym", "muname", "acres", "wkt"}
        if any(not required.issubset(row) for row in map_rows):
            raise SoilsSourceError("SDA map-unit response is missing required columns.")
        mukeys = tuple(dict.fromkeys(str(row["mukey"]) for row in map_rows))
        if any(not MUKEY_PATTERN.fullmatch(mukey) for mukey in mukeys):
            raise SoilsSourceError("SDA returned a non-numeric mukey.")
        started = time.perf_counter()
        component_rows = _post(
            QUERY_B.format(mukeys=", ".join(f"'{mukey}'" for mukey in mukeys)),
            http_client,
            "component",
        )
        timings["component_query"] = time.perf_counter() - started
        components: dict[str, list[SoilComponent]] = {mukey: [] for mukey in mukeys}
        for row in component_rows:
            component = _component(row)
            components[component.mukey].append(component)
        started = time.perf_counter()
        units: list[SoilUnitResult] = []
        for row in map_rows:
            geometry = wkt.loads(str(row["wkt"])).intersection(parcel_geometry)
            if geometry.is_empty:
                continue
            unit_acres = acreage(geometry)
            unit_components = tuple(components.get(str(row["mukey"]), ()))
            dominant = max(unit_components, key=lambda value: value.percent or -1) if unit_components else None
            units.append(SoilUnitResult(
                geometry=geometry,
                mukey=str(row["mukey"]),
                musym=None if row["musym"] is None else str(row["musym"]),
                map_unit_name=None if row["muname"] is None else str(row["muname"]),
                acres=unit_acres,
                parcel_percent=unit_acres / parcel_acres * 100 if parcel_acres else 0,
                reported_acres=_number(row, "acres"),
                dominant_component=dominant,
                components=unit_components,
            ))
        timings["clipping_and_metrics"] = time.perf_counter() - started
        covered = sum(unit.acres for unit in units)
        warnings: list[dict[str, Any]] = []
        warnings.extend(aoi_warnings)
        if aoi_acres and covered / aoi_acres < 0.99:
            warnings.append({
                "code": "soils_coverage_incomplete",
                "message": "SSURGO clipped map units cover less than 99% of the submitted SDA AOI.",
                "coverage_fraction": covered / aoi_acres,
            })
        hydrologic_groups: dict[str, float] = {}
        drainage: dict[str, float] = {}
        slopes = [(unit.acres, unit.dominant_component.slope_representative)
                  for unit in units if unit.dominant_component and unit.dominant_component.slope_representative is not None]
        for unit in units:
            dominant = unit.dominant_component
            if dominant and dominant.hydrologic_group:
                hydrologic_groups[dominant.hydrologic_group] = hydrologic_groups.get(dominant.hydrologic_group, 0) + unit.acres
            if dominant and dominant.drainage_class:
                drainage[dominant.drainage_class] = drainage.get(dominant.drainage_class, 0) + unit.acres
        metrics: dict[str, Any] = {
            "parcel_acres": parcel_acres,
            "covered_acres": covered,
            "aoi_acres": aoi_acres,
            "coverage_fraction": covered / aoi_acres if aoi_acres else 0,
            "aoi_geometry_method": "native_wkt" if not aoi_warnings else aoi_warnings[-1]["aoi_geometry_method"],
            "hydrologic_group_acres": hydrologic_groups,
            "dominant_hydrologic_group": (
                max(hydrologic_groups, key=lambda name: hydrologic_groups[name])
                if hydrologic_groups else None
            ),
            "drainage_class_acres": drainage,
            "area_weighted_representative_slope": (
                sum(area * slope for area, slope in slopes) / sum(area for area, _ in slopes)
                if slopes else None
            ),
            "surface_ksat_min": min((unit.dominant_component.ksat for unit in units if unit.dominant_component and unit.dominant_component.ksat is not None), default=None),
            "surface_ksat_max": max((unit.dominant_component.ksat for unit in units if unit.dominant_component and unit.dominant_component.ksat is not None), default=None),
            "sda_reported_acres": sum(unit.reported_acres or 0 for unit in units),
            "preliminary_planning_only": True,
        }
        return SoilsResult(units, metrics, warnings, SDA_URL, retrieved_at, timings)
    finally:
        if close_client:
            http_client.close()
