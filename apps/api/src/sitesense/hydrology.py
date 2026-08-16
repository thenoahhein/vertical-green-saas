"""WhiteboxTools-backed, window-scoped DEM hydrology calculations."""

from __future__ import annotations

import json
import math
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import geopandas as gpd
import httpx
import numpy as np
import rasterio
from rasterio.features import geometry_mask, shapes
from rasterio.transform import Affine
from shapely.geometry import LineString, MultiLineString, Polygon, shape
from shapely.ops import linemerge, unary_union

from sitesense.terrain import valid_data_mask

ACRE_SQUARE_METERS = 4046.8564224
DEFAULT_STREAM_THRESHOLD_CELLS = 100
DEFAULT_WINDOW_INFLOW_THRESHOLD_CELLS = 100
MIN_DEPRESSION_AREA_M2 = 9.0
MIN_DEPRESSION_DEPTH_M = 0.3
MIN_CATCHMENT_AREA_M2 = 100.0
MIN_RIDGE_VALLEY_LENGTH_M = 1.0
MAX_RETAINED_POLYGONS = 5_000
MAX_RETAINED_LINES = 5_000
HYDROGRAPHY_URL = (
    "https://3dhp.nationalmap.gov/arcgis/rest/services/usgs_3dhp_all/FeatureServer"
)
WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer"


class HydrologySourceError(RuntimeError):
    """An upstream hydrography service or raster source could not be read."""


class WhiteboxBinaryError(RuntimeError):
    """The image does not contain the warmed WhiteboxTools executable."""


class JsonClient(Protocol):
    def get(self, url: str, **kwargs: Any) -> httpx.Response: ...

    def close(self) -> None: ...


@dataclass
class HydroCorridor:
    geometry: LineString | MultiLineString
    contributing_acres: float
    parcel_length_m: float
    flow_direction_degrees: float | None
    mapped_water_relationship: str


@dataclass
class HydroDepression:
    geometry: Polygon
    depth_m: float
    volume_m3: float


@dataclass
class HydrologyResult:
    conditioned: np.ndarray
    flow_direction: np.ndarray
    flow_accumulation: np.ndarray
    drainage_lines: list[LineString | MultiLineString]
    catchments: list[Polygon]
    depressions: list[HydroDepression]
    ridgelines: list[LineString | MultiLineString]
    valleys: list[LineString | MultiLineString]
    corridors: list[HydroCorridor]
    metrics: dict[str, float | int | str | bool | None]
    warnings: list[dict[str, object]]
    transform: Affine
    crs: str
    stage_timings: dict[str, float]


def whitebox_binary_path() -> Path:
    try:
        import whitebox
    except ImportError as exc:
        raise WhiteboxBinaryError("WhiteboxTools Python package is not installed.") from exc
    package_dir = Path(whitebox.__file__).resolve().parent
    candidates = (package_dir / "WBT" / "whitebox_tools", package_dir / "whitebox_tools")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise WhiteboxBinaryError(
        "WhiteboxTools binary is missing; image build must warm it before requests."
    )


def _write_raster(
    path: Path,
    array: np.ndarray,
    transform: Affine,
    crs: str,
    nodata: float,
) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="DEFLATE",
    ) as dataset:
        dataset.write(np.asarray(array, dtype="float32"), 1)


def _read_raster(path: Path) -> np.ndarray:
    with rasterio.open(path) as dataset:
        values = np.asarray(dataset.read(1), dtype="float32")
        if dataset.nodata is not None:
            values[~valid_data_mask(values, float(dataset.nodata))] = np.nan
        return values


def _vectorize_mask(
    values: np.ndarray,
    transform: Affine,
    predicate: Any,
) -> list[Polygon]:
    result: list[Polygon] = []
    mask = predicate(values)
    for geometry, value in shapes(
        np.ones(values.shape, dtype="uint8"),
        mask=mask,
        transform=transform,
    ):
        if value:
            candidate = shape(geometry)
            if isinstance(candidate, Polygon) and not candidate.is_empty:
                result.append(candidate)
    return result


def _line_parts(geometry: Any) -> list[LineString | MultiLineString]:
    if geometry.geom_type in {"LineString", "MultiLineString"}:
        return [geometry]
    if hasattr(geometry, "geoms"):
        parts: list[LineString | MultiLineString] = []
        for part in geometry.geoms:
            parts.extend(_line_parts(part))
        return parts
    return []


def _line_direction_degrees(line: LineString | MultiLineString) -> float | None:
    coordinates = list(line.coords) if isinstance(line, LineString) else list(line.geoms[0].coords)
    if len(coordinates) < 2:
        return None
    x1, y1 = coordinates[0]
    x2, y2 = coordinates[-1]
    return float((math.degrees(math.atan2(x2 - x1, y2 - y1)) + 360.0) % 360.0)


def whitebox_binary_version() -> str:
    """Return the warmed WhiteboxTools version without downloading anything."""
    whitebox_binary_path()
    from whitebox import WhiteboxTools

    return str(WhiteboxTools().version()).splitlines()[0]


def _raster_mask_to_lines(
    values: np.ndarray,
    transform: Affine,
) -> list[LineString | MultiLineString]:
    """Trace active raster cells through their centers as a line network."""
    active = values > 0
    rows, cols = np.where(active)
    segments: list[LineString] = []
    for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
        x1, y1 = transform * (col + 0.5, row + 0.5)
        if col + 1 < active.shape[1] and active[row, col + 1]:
            x2, y2 = transform * (col + 1.5, row + 0.5)
            segments.append(LineString([(x1, y1), (x2, y2)]))
        if row + 1 < active.shape[0] and active[row + 1, col]:
            x2, y2 = transform * (col + 0.5, row + 1.5)
            segments.append(LineString([(x1, y1), (x2, y2)]))
    if not segments:
        return []
    return _line_parts(linemerge(unary_union(segments)))


def _corridor_contributing_acres(
    line: LineString | MultiLineString,
    flow_accumulation: np.ndarray,
    transform: Affine,
    pixel_area: float,
) -> float:
    pixel = max(abs(transform.a), abs(transform.e)) / 2
    mask = geometry_mask(
        [line.buffer(pixel)],
        out_shape=flow_accumulation.shape,
        transform=transform,
        invert=True,
    )
    values = flow_accumulation[mask & valid_data_mask(flow_accumulation, np.nan)]
    if not values.size:
        return 0.0
    return float(np.max(values) * pixel_area / ACRE_SQUARE_METERS)


def boundary_inflow_mask(flow_direction: np.ndarray) -> np.ndarray:
    """Return boundary cells whose D8 pointer directs flow into the window."""
    inward = np.zeros(flow_direction.shape, dtype=bool)
    inward[0, :] |= np.isin(flow_direction[0, :], (32, 64, 128))
    inward[-1, :] |= np.isin(flow_direction[-1, :], (2, 4, 8))
    inward[:, 0] |= np.isin(flow_direction[:, 0], (1, 2, 128))
    inward[:, -1] |= np.isin(flow_direction[:, -1], (8, 16, 32))
    return inward


def _filter_lines_by_length(
    lines: list[LineString | MultiLineString],
    minimum_length_m: float,
) -> list[LineString | MultiLineString]:
    return [line for line in lines if line.length >= minimum_length_m]


def _cap_polygons(polygons: list[Polygon]) -> tuple[list[Polygon], bool]:
    if len(polygons) <= MAX_RETAINED_POLYGONS:
        return polygons, False
    return sorted(polygons, key=lambda polygon: polygon.area, reverse=True)[:MAX_RETAINED_POLYGONS], True


def _cap_lines(
    lines: list[LineString | MultiLineString],
) -> tuple[list[LineString | MultiLineString], bool]:
    if len(lines) <= MAX_RETAINED_LINES:
        return lines, False
    return sorted(lines, key=lambda line: line.length, reverse=True)[:MAX_RETAINED_LINES], True


def _depression_features(
    polygons: list[Polygon],
    elevation: np.ndarray,
    conditioned: np.ndarray,
    transform: Affine,
    pixel_area: float,
) -> list[HydroDepression]:
    features: list[HydroDepression] = []
    for polygon in polygons:
        if polygon.area < MIN_DEPRESSION_AREA_M2:
            continue
        mask = geometry_mask(
            [polygon],
            out_shape=elevation.shape,
            transform=transform,
            invert=True,
        )
        valid_cells = valid_data_mask(conditioned, np.nan) & valid_data_mask(elevation, np.nan)
        depth = np.zeros(conditioned.shape, dtype="float64")
        np.subtract(
            conditioned.astype("float64"),
            elevation.astype("float64"),
            out=depth,
            where=valid_cells,
        )
        np.maximum(depth, 0.0, out=depth)
        depths = depth[mask & valid_data_mask(depth, np.nan)]
        if not depths.size or float(np.max(depths)) < MIN_DEPRESSION_DEPTH_M:
            continue
        features.append(
            HydroDepression(
                geometry=polygon,
                depth_m=float(np.max(depths)),
                volume_m3=float(np.sum(depths) * pixel_area),
            )
        )
    return features


def assign_mapped_water_relationships(
    result: HydrologyResult,
    mapped_geometries: list[Any] | None,
    *,
    tolerance_m: float = 30.0,
) -> None:
    """Annotate corridors against projected 3DHP flowline/waterbody geometry."""
    if mapped_geometries is None:
        for corridor in result.corridors:
            corridor.mapped_water_relationship = "3DHP hydrography unavailable"
    else:
        for corridor in result.corridors:
            corridor.mapped_water_relationship = (
                "near mapped 3DHP flowline/waterbody"
                if any(
                    corridor.geometry.distance(geometry) <= tolerance_m
                    for geometry in mapped_geometries
                )
                else "no mapped 3DHP hydrography nearby"
            )
    result.metrics["mapped_water_tolerance_m"] = tolerance_m


def _dissolve_polygons(polygons: list[Polygon]) -> list[Polygon]:
    if not polygons:
        return []
    dissolved = unary_union(polygons)
    if isinstance(dissolved, Polygon):
        return [dissolved]
    if dissolved.geom_type == "MultiPolygon":
        return list(dissolved.geoms)
    return []


def _read_vector_lines(path: Path) -> list[LineString | MultiLineString]:
    if not path.exists():
        return []
    try:
        if path.suffix.lower() == ".shp":
            return [
                geometry
                for geometry in gpd.read_file(path).geometry
                if geometry is not None
                and geometry.geom_type in {"LineString", "MultiLineString"}
            ]
        payload = json.loads(path.read_text())
        features = payload.get("features", [])
        return [
            line
            for feature in features
            if (line := shape(feature["geometry"])).geom_type in {"LineString", "MultiLineString"}
        ]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise HydrologySourceError(f"Whitebox vector output could not be read: {exc}") from exc


def run_hydrology(
    elevation: np.ndarray,
    transform: Affine,
    crs: str,
    parcel_geometry: Any,
    parcel_acres: float | None,
    *,
    stream_threshold_cells: int = DEFAULT_STREAM_THRESHOLD_CELLS,
    inflow_threshold_cells: int = DEFAULT_WINDOW_INFLOW_THRESHOLD_CELLS,
) -> HydrologyResult:
    """Run the complete local routing workflow in a cleaned per-job directory."""
    workflow_started = time.perf_counter()
    stage_timings: dict[str, float] = {}
    binary_version = whitebox_binary_version()
    valid = valid_data_mask(elevation)
    if not valid.any():
        raise HydrologySourceError("The DEM contains no valid cells for hydrology.")
    nodata = -9999.0
    vectorization_method = "raster-cell-centerline"
    with tempfile.TemporaryDirectory(prefix="sitesense-hydrology-") as directory:
        routing_started = time.perf_counter()
        root = Path(directory)
        dem = root / "dem.tif"
        conditioned_path = root / "conditioned.tif"
        pointer_path = root / "flow_direction.tif"
        accumulation_path = root / "flow_accumulation.tif"
        streams_path = root / "streams.tif"
        streams_vector_path = root / "drainage_lines.shp"
        subbasins_path = root / "catchments.tif"
        sinks_path = root / "depressions.tif"
        ridges_path = root / "ridgelines.tif"
        valleys_path = root / "valleys.tif"
        _write_raster(dem, np.where(valid, elevation, nodata), transform, crs, nodata)
        try:
            from whitebox import WhiteboxTools

            tools = WhiteboxTools()
            tools.set_verbose_mode(False)
            if tools.fill_depressions_wang_and_liu(str(dem), str(conditioned_path)) != 0:
                raise HydrologySourceError("WhiteboxTools fill_depressions_wang_and_liu failed.")
            if tools.d8_pointer(str(conditioned_path), str(pointer_path)) != 0:
                raise HydrologySourceError("WhiteboxTools d8_pointer failed.")
            if tools.d8_flow_accumulation(
                str(pointer_path), str(accumulation_path), out_type="cells", pntr=True
            ) != 0:
                raise HydrologySourceError("WhiteboxTools d8_flow_accumulation failed.")
            if tools.extract_streams(
                str(accumulation_path), str(streams_path), stream_threshold_cells, zero_background=True
            ) != 0:
                raise HydrologySourceError("WhiteboxTools extract_streams failed.")
            if tools.raster_streams_to_vector(
                str(streams_path), str(pointer_path), str(streams_vector_path)
            ) != 0:
                raise HydrologySourceError("WhiteboxTools raster_streams_to_vector failed.")
            if tools.subbasins(str(pointer_path), str(streams_path), str(subbasins_path)) != 0:
                raise HydrologySourceError("WhiteboxTools subbasins failed.")
            if tools.sink(str(dem), str(sinks_path), zero_background=True) != 0:
                raise HydrologySourceError("WhiteboxTools sink failed.")
            if tools.find_ridges(str(conditioned_path), str(ridges_path)) != 0:
                raise HydrologySourceError("WhiteboxTools find_ridges failed.")
            if tools.extract_valleys(str(conditioned_path), str(valleys_path)) != 0:
                raise HydrologySourceError("WhiteboxTools extract_valleys failed.")
        except WhiteboxBinaryError:
            raise
        except HydrologySourceError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise HydrologySourceError(f"WhiteboxTools hydrology failed: {exc}") from exc
        stage_timings["whitebox_routing"] = time.perf_counter() - routing_started
        conditioned = _read_raster(conditioned_path)
        flow_direction = _read_raster(pointer_path)
        flow_accumulation = _read_raster(accumulation_path)
        streams = _read_raster(streams_path)
        subbasins = _read_raster(subbasins_path)
        ridges = _read_raster(ridges_path)
        valley_raster = _read_raster(valleys_path)
        drainage_lines = _read_vector_lines(streams_vector_path)
        if drainage_lines:
            vectorization_method = "whitebox-raster-streams-to-vector"

    pixel_area = abs(transform.a * transform.e)
    stream_mask = streams > 0
    if not drainage_lines:
        drainage_lines = _raster_mask_to_lines(streams, transform)
    boundary = np.zeros(stream_mask.shape, dtype=bool)
    boundary[0, :] = boundary[-1, :] = True
    boundary[:, 0] = boundary[:, -1] = True
    boundary_valid = boundary & valid_data_mask(flow_accumulation, np.nan)
    inflow = boundary_inflow_mask(flow_direction) & boundary_valid
    boundary_accumulation = flow_accumulation[boundary_valid]
    inflow_accumulation = flow_accumulation[inflow]
    inflow_cells = int((inflow_accumulation >= inflow_threshold_cells).sum())
    max_boundary_accumulation = (
        int(np.nanmax(boundary_accumulation)) if boundary_accumulation.size else 0
    )
    stream_values = flow_accumulation[stream_mask & valid_data_mask(flow_accumulation, np.nan)]
    local_contributing_acres = (
        float(np.nanmax(stream_values) * pixel_area / ACRE_SQUARE_METERS)
        if stream_values.size
        else 0.0
    )
    warnings: list[dict[str, object]] = []
    truncated = inflow_cells > 0
    if truncated:
        warnings.append(
            {
                "code": "hydrology_window_truncated",
                "message": "Significant flow enters the analysis window boundary; contributing acreage is a lower bound.",
                "boundary_inflow_cells": inflow_cells,
                "boundary_inflow_max_cells": max_boundary_accumulation,
                "contributing_acres_is_lower_bound": True,
            }
        )
    filtering_started = time.perf_counter()
    raw_catchments = [
        polygon
        for polygon in _vectorize_mask(subbasins, transform, lambda values: values > 0)
        if polygon.area >= MIN_CATCHMENT_AREA_M2
    ]
    raw_catchments, catchments_capped = _cap_polygons(raw_catchments)
    valid_depth_cells = (
        valid_data_mask(conditioned, np.nan)
        & valid_data_mask(elevation, np.nan)
    )
    depression_depth = np.zeros(conditioned.shape, dtype="float64")
    np.subtract(
        conditioned.astype("float64"),
        elevation.astype("float64"),
        out=depression_depth,
        where=valid_depth_cells,
    )
    np.maximum(depression_depth, 0.0, out=depression_depth)
    raw_depressions = _vectorize_mask(
        depression_depth, transform, lambda values: values >= MIN_DEPRESSION_DEPTH_M
    )
    raw_depressions, depressions_capped = _cap_polygons(raw_depressions)
    catchments = raw_catchments
    depressions = _depression_features(
        raw_depressions, elevation, conditioned, transform, pixel_area
    )
    ridgelines = _filter_lines_by_length(
        _raster_mask_to_lines(ridges, transform), MIN_RIDGE_VALLEY_LENGTH_M
    )
    valleys = _filter_lines_by_length(
        _raster_mask_to_lines(valley_raster, transform), MIN_RIDGE_VALLEY_LENGTH_M
    )
    ridgelines, ridges_capped = _cap_lines(ridgelines)
    valleys, valleys_capped = _cap_lines(valleys)
    if catchments_capped or depressions_capped or ridges_capped or valleys_capped:
        warnings.append(
            {
                "code": "hydrology_products_capped",
                "message": "Hydrology products exceeded the retained-feature cap; least significant features were omitted.",
                "max_retained_polygons": MAX_RETAINED_POLYGONS,
                "max_retained_lines": MAX_RETAINED_LINES,
            }
        )
    stage_timings["vectorization_filtering"] = time.perf_counter() - filtering_started
    corridors: list[HydroCorridor] = []
    for line in drainage_lines:
        parcel_intersection = line.intersection(parcel_geometry)
        corridors.append(
            HydroCorridor(
                geometry=line,
                contributing_acres=_corridor_contributing_acres(
                    line, flow_accumulation, transform, pixel_area
                ),
                parcel_length_m=(
                    float(parcel_intersection.length)
                    if not parcel_intersection.is_empty
                    else 0.0
                ),
                flow_direction_degrees=_line_direction_degrees(line),
                mapped_water_relationship="3DHP hydrography unavailable",
            )
        )
    metrics: dict[str, float | int | str | bool | None] = {
        "analysis_window_pixel_area_m2": float(pixel_area),
        "stream_threshold_cells": stream_threshold_cells,
        "window_boundary_inflow_cells": inflow_cells,
        "window_boundary_inflow_max_cells": max_boundary_accumulation,
        "window_boundary_inflow_max_inward_cells": (
            int(np.nanmax(inflow_accumulation)) if inflow_accumulation.size else 0
        ),
        "window_truncated": truncated,
        "contributing_acres_within_window": local_contributing_acres,
        "contributing_acres_is_lower_bound": truncated,
        "analysis_scope": "within analysis window",
        "local_depression_count": len(raw_depressions),
        "filtered_depression_count": len(depressions),
        "ridge_segment_count": len(ridgelines),
        "valley_segment_count": len(valleys),
        "drainage_line_count": len(drainage_lines),
        "catchment_count": len(catchments),
        "filtered_catchment_count": len(catchments),
        "depression_min_area_m2": MIN_DEPRESSION_AREA_M2,
        "depression_min_depth_m": MIN_DEPRESSION_DEPTH_M,
        "catchment_min_area_m2": MIN_CATCHMENT_AREA_M2,
        "ridge_valley_min_length_m": MIN_RIDGE_VALLEY_LENGTH_M,
        "valid_cell_count": int(np.count_nonzero(valid)),
        "max_flow_accumulation_cells": float(np.nanmax(flow_accumulation)),
        "parcel_acres": parcel_acres,
        "potential_water_management_review_required": True,
        "whitebox_binary_version": binary_version,
        "drainage_vectorization_method": vectorization_method,
        "routing_resolution_m": abs(transform.a),
    }
    stage_timings["hydrology_total"] = time.perf_counter() - workflow_started
    return HydrologyResult(
        conditioned=conditioned,
        flow_direction=flow_direction,
        flow_accumulation=flow_accumulation,
        drainage_lines=drainage_lines,
        catchments=catchments,
        depressions=depressions,
        ridgelines=ridgelines,
        valleys=valleys,
        corridors=corridors,
        metrics=metrics,
        warnings=warnings,
        transform=transform,
        crs=crs,
        stage_timings=stage_timings,
    )


def _arc_geometry(value: dict[str, Any]) -> Any:
    if "x" in value and "y" in value:
        return {"type": "Point", "coordinates": [value["x"], value["y"]]}
    if "paths" in value:
        return {"type": "MultiLineString", "coordinates": value["paths"]}
    if "rings" in value:
        return {"type": "Polygon", "coordinates": value["rings"]}
    return None


def _query_arcgis(
    url: str,
    bbox: tuple[float, float, float, float],
    client: JsonClient,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{url}/query",
        params={
            "f": "json",
            "where": "1=1",
            "geometry": ",".join(str(value) for value in bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "resultRecordCount": 2500,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise HydrologySourceError(f"ArcGIS query failed: {payload['error']}")
    return cast(list[dict[str, Any]], payload.get("features", []))


def fetch_3dhp(
    bbox: tuple[float, float, float, float],
    client: JsonClient | None = None,
) -> dict[int, list[dict[str, Any]]]:
    http_client = cast(JsonClient, client or httpx.Client(timeout=8.0))
    try:
        layers = (20, 30, 40, 50, 60, 80)
        with ThreadPoolExecutor(max_workers=len(layers)) as executor:
            responses = executor.map(
                lambda layer: _query_arcgis(HYDROGRAPHY_URL + f"/{layer}", bbox, http_client),
                layers,
            )
            return dict(zip(layers, responses, strict=True))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise HydrologySourceError(f"3DHP query failed: {exc}") from exc
    finally:
        if client is None:
            http_client.close()


def fetch_wbd_membership(
    longitude: float,
    latitude: float,
    client: JsonClient | None = None,
) -> dict[str, Any]:
    http_client = cast(JsonClient, client or httpx.Client(timeout=30.0))
    try:
        result: dict[str, Any] = {}
        for layer, code in ((5, "huc10"), (6, "huc12")):
            response = http_client.get(
                f"{WBD_URL}/{layer}/query",
                params={
                    "f": "json",
                    "where": "1=1",
                    "geometry": f"{longitude},{latitude}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "*",
                    "returnGeometry": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise HydrologySourceError(f"WBD query failed: {payload['error']}")
            attributes = (payload.get("features") or [{}])[0].get("attributes", {})
            result[code] = attributes
        return result
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise HydrologySourceError(f"WBD query failed: {exc}") from exc
    finally:
        if client is None:
            http_client.close()


def feature_geometries(features: list[dict[str, Any]]) -> list[Any]:
    geometries: list[Any] = []
    for feature in features:
        geometry = _arc_geometry(feature.get("geometry", {}))
        if geometry is not None:
            geometries.append(shape(geometry))
    return geometries
