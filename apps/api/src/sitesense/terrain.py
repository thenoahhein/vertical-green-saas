"""Deterministic terrain calculations for the first analysis category."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

import httpx
import numpy as np
import rasterio
from matplotlib import pyplot as plt
from matplotlib import use as matplotlib_use
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine, from_origin
from rasterio.warp import reproject, transform_bounds
from shapely.geometry import LineString, MultiLineString, box, mapping
from shapely.ops import unary_union

matplotlib_use("Agg")

ACRE_SQUARE_METERS = 4046.8564224
DEFAULT_TERRAIN_BUFFER_METERS = 500.0
ONE_METER_DATASET = "Digital Elevation Model (DEM) 1 meter"
THIRD_ARC_SECOND_DATASET = "National Elevation Dataset (NED) 1/3 arc-second"
NODATA = -3.4028230607370965e38
MAX_ANALYSIS_CELLS = 9_000_000


def valid_data_mask(values: np.ndarray, nodata: float = NODATA) -> np.ndarray:
    """Return cells that are finite and distinct from the declared nodata value."""
    finite = np.isfinite(values)
    if math.isnan(nodata):
        return finite
    tolerance = max(abs(nodata) * 1e-7, 1e-6)
    difference = np.abs(values.astype("float64") - float(nodata))
    return finite & (difference > tolerance)


class TerrainSourceError(RuntimeError):
    """An upstream DEM catalog or raster source could not be read."""


class ProductClient(Protocol):
    def get(self, url: str, **kwargs: Any) -> httpx.Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class TerrainProduct:
    title: str
    dataset_name: str
    source_url: str
    bounds: tuple[float, float, float, float]
    spatial_resolution: str
    published_at: datetime | None
    byte_size: int | None


@dataclass(frozen=True)
class TerrainSelection:
    products: tuple[TerrainProduct, ...]
    used_fallback: bool
    warning: str | None = None


def cached_products_for_bounds(
    products: tuple[TerrainProduct, ...],
    bbox: tuple[float, float, float, float],
) -> TerrainSelection:
    requested = box(*bbox)
    candidates = tuple(product for product in products if box(*product.bounds).intersects(requested))
    coverage = unary_union([box(*product.bounds) for product in candidates]) if candidates else None
    if coverage is None or not coverage.covers(requested):
        return TerrainSelection(
            (),
            True,
            "terrain_source_unavailable: TNMAccess catalog was unavailable and no cached 3DEP product covers the buffered parcel.",
        )
    return TerrainSelection(
        candidates,
        True,
        "terrain_catalog_unavailable_cached_product: TNMAccess catalog was unavailable; used a cached 3DEP product record.",
    )


@dataclass
class TerrainResult:
    elevation: np.ndarray
    slope_percent: np.ndarray
    slope_degrees: np.ndarray
    aspect_degrees: np.ndarray
    hillshade: np.ndarray
    transform: Affine
    crs: str
    parcel_mask: np.ndarray
    buffer_mask: np.ndarray
    metrics: dict[str, Any]
    contours: list[tuple[float, bool, LineString | MultiLineString]]
    coverage_fraction: float
    warning: dict[str, Any] | None


def _product_bounds(item: dict[str, Any]) -> tuple[float, float, float, float]:
    bounds = item.get("boundingBox") or {}
    if isinstance(bounds, dict):
        keys = (
            ("minX", "minY", "maxX", "maxY"),
            ("west", "south", "east", "north"),
        )
        for names in keys:
            if all(name in bounds for name in names):
                return tuple(float(bounds[name]) for name in names)  # type: ignore[return-value]
    for key in ("bbox", "bounds"):
        value = item.get(key)
        if isinstance(value, list) and len(value) >= 4:
            return tuple(float(part) for part in value[:4])  # type: ignore[return-value]
    raise ValueError(f"TNMAccess product has no usable bounding box: {item!r}")


def _parse_product(item: dict[str, Any], dataset_name: str) -> TerrainProduct:
    published = item.get("publicationDate") or item.get("pubDate")
    published_at = None
    if published:
        try:
            published_at = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        except ValueError:
            published_at = None
    byte_size = item.get("sizeInBytes")
    return TerrainProduct(
        title=str(item.get("title") or dataset_name),
        dataset_name=dataset_name,
        source_url=str(item["downloadURL"]),
        bounds=_product_bounds(item),
        spatial_resolution="1 m" if dataset_name == ONE_METER_DATASET else "1/3 arc-second",
        published_at=published_at,
        byte_size=int(byte_size) if byte_size is not None else None,
    )


def _query_products(
    bbox: tuple[float, float, float, float],
    dataset_name: str,
    client: ProductClient,
) -> list[TerrainProduct]:
    response = client.get(
        "https://tnmaccess.nationalmap.gov/api/v1/products",
        params={
            "bbox": ",".join(str(value) for value in bbox),
            "datasets": dataset_name,
            "prodFormats": "GeoTIFF",
            "max": 1000,
        },
    )
    response.raise_for_status()
    payload = response.json()
    return [
        product
        for product in (_parse_product(item, dataset_name) for item in payload.get("items", []))
        if box(*product.bounds).intersects(box(*bbox))
    ]


def _query_products_with_retry(
    bbox: tuple[float, float, float, float],
    dataset_name: str,
    client: ProductClient,
    *,
    attempts: int = 2,
) -> list[TerrainProduct]:
    last_error: httpx.HTTPError | None = None
    for attempt in range(attempts):
        try:
            return _query_products(bbox, dataset_name, client)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1)
    assert last_error is not None
    raise last_error


def select_products(
    bbox: tuple[float, float, float, float],
    client: ProductClient | None = None,
) -> TerrainSelection:
    """Prefer 1 m products and use 1/3 arc-second only for incomplete coverage."""
    http_client: ProductClient = cast(ProductClient, client or httpx.Client(timeout=8.0))
    close_client = client is None
    try:
        try:
            one_meter = _query_products_with_retry(bbox, ONE_METER_DATASET, http_client)
        except httpx.HTTPError as exc:
            raise TerrainSourceError(f"TNMAccess 1-meter query failed: {exc}") from exc
        requested = box(*bbox)
        coverage = box(*bbox)
        if one_meter:
            coverage = box(*one_meter[0].bounds)
            for product in one_meter[1:]:
                coverage = coverage.union(box(*product.bounds))
        if one_meter and coverage.covers(requested):
            return TerrainSelection(tuple(one_meter), False)
        try:
            fallback = _query_products_with_retry(bbox, THIRD_ARC_SECOND_DATASET, http_client)
        except httpx.HTTPError as exc:
            raise TerrainSourceError(f"TNMAccess fallback query failed: {exc}") from exc
        if fallback:
            fallback_coverage = box(*fallback[0].bounds)
            for product in fallback[1:]:
                fallback_coverage = fallback_coverage.union(box(*product.bounds))
            if fallback_coverage.covers(requested):
                return TerrainSelection(
                    tuple(fallback),
                    True,
                    "1 m 3DEP coverage did not fully cover the buffered parcel; used 1/3 arc-second DEM.",
                )
        return TerrainSelection(
            tuple(one_meter),
            False,
            "No 3DEP product fully covers the buffered parcel.",
        )
    finally:
        if close_client:
            http_client.close()


def _target_grid(
    bounds: tuple[float, float, float, float],
    resolution: float,
) -> tuple[Affine, int, int]:
    min_x, min_y, max_x, max_y = bounds
    width = max(1, int(math.ceil((max_x - min_x) / resolution)))
    height = max(1, int(math.ceil((max_y - min_y) / resolution)))
    return from_origin(min_x, max_y, resolution, resolution), width, height


def _source_resolution(dataset: rasterio.DatasetReader) -> float:
    if dataset.crs and dataset.crs.is_projected:
        return float(abs(cast(float, dataset.res[0])))
    lonlat_width = abs(cast(float, dataset.res[0])) * 111_320.0
    return max(1.0, lonlat_width)


def read_mosaic(
    products: tuple[TerrainProduct, ...],
    target_bounds_wgs84: tuple[float, float, float, float],
    target_crs: str,
) -> tuple[np.ndarray, Affine, str, tuple[str, ...]]:
    """Window-read product COGs and mosaic them on one nodata-aware grid."""
    if not products:
        raise ValueError("No terrain products available")
    target_bounds = transform_bounds("EPSG:4326", target_crs, *target_bounds_wgs84)
    ordered_products = sorted(
        products,
        key=lambda product: product.published_at.timestamp() if product.published_at else float("-inf"),
        reverse=True,
    )
    try:
        with rasterio.open(ordered_products[0].source_url) as first:
            resolution = _source_resolution(first)
    except (rasterio.errors.RasterioError, OSError) as exc:
        raise TerrainSourceError(f"3DEP raster read failed: {exc}") from exc
    target_transform, width, height = _target_grid(target_bounds, resolution)
    if width * height > MAX_ANALYSIS_CELLS:
        raise TerrainSourceError(
            f"Terrain analysis grid has {width * height:,} cells; "
            f"the maximum supported size is {MAX_ANALYSIS_CELLS:,} "
            f"(dimensions {width}x{height} at {resolution:g} m)."
        )
    destination = np.full((height, width), NODATA, dtype="float32")
    filled = np.zeros(destination.shape, dtype=bool)
    contributors: list[str] = []
    try:
        for product in ordered_products:
            with rasterio.open(product.source_url) as source:
                source_bounds = transform_bounds(
                    target_crs, source.crs, *target_bounds, densify_pts=21
                )
                window = rasterio.windows.from_bounds(*source_bounds, transform=source.transform)
                window = window.round_offsets().round_lengths()
                window = window.intersection(rasterio.windows.Window(0, 0, source.width, source.height))
                if window.width <= 0 or window.height <= 0:
                    continue
                source_array = source.read(1, window=window, masked=False)
                source_transform = source.window_transform(window)
                reprojected = np.full(destination.shape, NODATA, dtype="float32")
                reproject(
                    source_array,
                    reprojected,
                    src_transform=source_transform,
                    src_crs=source.crs,
                    src_nodata=source.nodata,
                    dst_transform=target_transform,
                    dst_crs=target_crs,
                    dst_nodata=NODATA,
                    resampling=Resampling.bilinear,
                )
                valid = valid_data_mask(reprojected) & ~filled
                if valid.any():
                    destination[valid] = reprojected[valid]
                    filled[valid] = True
                    contributors.append(product.source_url)
    except (rasterio.errors.RasterioError, OSError) as exc:
        raise TerrainSourceError(f"3DEP raster read failed: {exc}") from exc
    return destination, target_transform, target_crs, tuple(contributors)


def _focal_mean(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    padded = np.pad(values, 1, mode="edge")
    valid_padded = np.pad(valid, 1, mode="constant", constant_values=False)
    total = np.zeros(values.shape, dtype="float64")
    count = np.zeros(values.shape, dtype="uint8")
    for row in range(3):
        for column in range(3):
            total += np.where(valid_padded[row : row + values.shape[0], column : column + values.shape[1]], padded[row : row + values.shape[0], column : column + values.shape[1]], 0)
            count += valid_padded[row : row + values.shape[0], column : column + values.shape[1]]
    result = np.full(values.shape, np.nan, dtype="float32")
    np.divide(total, count, out=result, where=count > 0)
    return result


def _horn_derivatives(values: np.ndarray, valid: np.ndarray, cell_size: float) -> tuple[np.ndarray, np.ndarray]:
    padded = np.pad(values, 1, mode="edge")
    valid_padded = np.pad(valid, 1, mode="constant", constant_values=False)
    neighborhoods = [
        valid_padded[row : row + values.shape[0], col : col + values.shape[1]]
        for row in range(3)
        for col in range(3)
    ]
    all_valid = np.logical_and.reduce(neighborhoods)
    dzdx = (
        padded[0:-2, 2:] + 2 * padded[1:-1, 2:] + padded[2:, 2:]
        - padded[0:-2, 0:-2] - 2 * padded[1:-1, 0:-2] - padded[2:, 0:-2]
    ) / (8 * cell_size)
    dzdy = (
        padded[2:, 0:-2] + 2 * padded[2:, 1:-1] + padded[2:, 2:]
        - padded[0:-2, 0:-2] - 2 * padded[0:-2, 1:-1] - padded[0:-2, 2:]
    ) / (8 * cell_size)
    return np.where(all_valid, dzdx, np.nan), np.where(all_valid, dzdy, np.nan)


def _metric_values(
    elevation: np.ndarray,
    slope_percent: np.ndarray,
    slope_degrees: np.ndarray,
    parcel_mask: np.ndarray,
    valid: np.ndarray,
    pixel_area: float,
    parcel_acres: float | None,
) -> tuple[dict[str, Any], float]:
    parcel_pixels = int(parcel_mask.sum())
    valid_parcel = parcel_mask & valid
    valid_pixels = int(valid_parcel.sum())
    coverage = valid_pixels / parcel_pixels if parcel_pixels else 0.0
    elevation_values = elevation[valid_parcel]
    slope_values = slope_percent[valid_parcel & valid_data_mask(slope_percent, np.nan)]
    slope_degree_values = slope_degrees[
        valid_parcel & valid_data_mask(slope_degrees, np.nan)
    ]
    if not len(elevation_values):
        return {
            "coverage_fraction": coverage,
            "parcel_acres": parcel_acres,
            "slope_histogram": [],
        }, coverage
    histogram: list[dict[str, float | str]] = []
    edges = (0.0, 2.0, 5.0, 10.0, 20.0, float("inf"))
    labels = ("0–2%", "2–5%", "5–10%", "10–20%", ">20%")
    for lower, upper, label in zip(edges[:-1], edges[1:], labels, strict=True):
        selected = (slope_values >= lower) & (slope_values < upper)
        acres = float(selected.sum() * pixel_area / ACRE_SQUARE_METERS)
        histogram.append(
            {
                "bucket": label,
                "acres": acres,
                "percentage": float(selected.sum() / len(slope_values) * 100),
                "percentage_denominator": "valid slope pixels",
            }
        )
    values_m = elevation_values.astype("float64")
    metrics: dict[str, Any] = {
        "coverage_fraction": coverage,
        "parcel_acres": parcel_acres,
        "valid_acres": float(valid_pixels * pixel_area / ACRE_SQUARE_METERS),
        "elevation_min_m": float(values_m.min()),
        "elevation_max_m": float(values_m.max()),
        "elevation_mean_m": float(values_m.mean()),
        "elevation_min_ft": float(values_m.min() * 3.280839895),
        "elevation_max_ft": float(values_m.max() * 3.280839895),
        "elevation_mean_ft": float(values_m.mean() * 3.280839895),
        "relief_m": float(values_m.max() - values_m.min()),
        "relief_ft": float((values_m.max() - values_m.min()) * 3.280839895),
        "mean_slope_percent": float(slope_values.mean()) if len(slope_values) else None,
        "mean_slope_degrees": float(slope_degree_values.mean()) if len(slope_degree_values) else None,
        "slope_histogram": histogram,
        "slope_statistics_surface": "3x3 focal-mean-smoothed elevation",
        "elevation_units": {"stored": "metres", "display": "feet"},
    }
    return metrics, coverage


def generate_contours(
    elevation: np.ndarray,
    transform: Affine,
    clip_geometry: Any,
    interval_feet: float,
) -> list[tuple[float, bool, LineString | MultiLineString]]:
    valid = valid_data_mask(elevation)
    if not valid.any():
        return []
    minimum = float(np.nanmin(elevation))
    maximum = float(np.nanmax(elevation))
    interval_m = interval_feet / 3.280839895
    first = math.ceil(minimum / interval_m) * interval_m
    levels = np.arange(first, maximum + interval_m / 2, interval_m)
    if len(levels) == 0:
        return []
    rows, columns = (cast(np.ndarray, values) for values in np.indices(elevation.shape))
    x_values = transform.c + (columns[0] + 0.5) * transform.a
    y_values = transform.f + (rows[:, 0] + 0.5) * transform.e
    figure, axis = plt.subplots()
    try:
        contour_set = axis.contour(x_values, y_values, np.where(valid, elevation, np.nan), levels=levels)
        results: list[tuple[float, bool, LineString | MultiLineString]] = []
        for level, segments in zip(contour_set.levels, contour_set.allsegs, strict=True):
            pieces = []
            for segment in segments:
                if len(segment) < 2:
                    continue
                clipped = LineString(segment).intersection(clip_geometry)
                if clipped.is_empty:
                    continue
                pieces.extend(_line_parts(clipped))
            if pieces:
                geometry = pieces[0] if len(pieces) == 1 else MultiLineString(pieces)
                level_feet = float(level * 3.280839895)
                is_index = math.isclose(level_feet / 10, round(level_feet / 10), abs_tol=1e-6)
                results.append((float(level), is_index, geometry))
        return results
    finally:
        plt.close(figure)


def _line_parts(geometry: Any) -> list[LineString]:
    if geometry.geom_type == "LineString":
        return [geometry]
    if hasattr(geometry, "geoms"):
        parts: list[LineString] = []
        for part in geometry.geoms:
            parts.extend(_line_parts(part))
        return parts
    return []


def analyze_elevation(
    elevation: np.ndarray,
    transform: Affine,
    crs: str,
    parcel_geometry: Any,
    buffer_geometry: Any,
    parcel_acres: float | None,
) -> TerrainResult:
    valid = valid_data_mask(elevation)
    clean_elevation = np.where(valid, elevation, np.nan).astype("float32")
    pixel_area = abs(transform.a * transform.e)
    parcel_mask = geometry_mask(
        [mapping(parcel_geometry)],
        out_shape=elevation.shape,
        transform=transform,
        invert=True,
    )
    buffer_mask = geometry_mask(
        [mapping(buffer_geometry)],
        out_shape=elevation.shape,
        transform=transform,
        invert=True,
    )
    smoothed = _focal_mean(clean_elevation, valid)
    smooth_valid = valid_data_mask(smoothed, np.nan)
    raw_dzdx, raw_dzdy = _horn_derivatives(clean_elevation, valid, abs(transform.a))
    stats_dzdx, stats_dzdy = _horn_derivatives(smoothed, smooth_valid, abs(transform.a))
    slope_percent = np.hypot(raw_dzdx, raw_dzdy) * 100
    slope_degrees = np.degrees(np.arctan(np.hypot(raw_dzdx, raw_dzdy)))
    stats_slope_percent = np.hypot(stats_dzdx, stats_dzdy) * 100
    stats_slope_degrees = np.degrees(np.arctan(np.hypot(stats_dzdx, stats_dzdy)))
    aspect = (np.degrees(np.arctan2(-raw_dzdx, raw_dzdy)) + 360) % 360
    slope_radians = np.arctan(np.hypot(raw_dzdx, raw_dzdy))
    azimuth = np.radians(315)
    altitude = np.radians(45)
    hillshade = (
        np.sin(altitude) * np.cos(slope_radians)
        + np.cos(altitude) * np.sin(slope_radians) * np.cos(azimuth - np.radians(aspect))
    ) * 255
    hillshade = np.where(valid, np.clip(hillshade, 0, 255), np.nan)
    metrics, coverage = _metric_values(
        clean_elevation,
        stats_slope_percent,
        stats_slope_degrees,
        parcel_mask,
        valid & smooth_valid,
        pixel_area,
        parcel_acres,
    )
    warning: dict[str, Any] | None = None
    if coverage == 0:
        warning = {
            "code": "terrain_source_unavailable",
            "message": "3DEP elevation coverage is unavailable for this parcel.",
            "missing_fraction": 1.0,
        }
    elif coverage < 0.99:
        warning = {
            "code": "terrain_coverage_incomplete",
            "message": f"3DEP elevation coverage is incomplete; {1 - coverage:.1%} of the parcel is missing.",
            "missing_fraction": 1 - coverage,
        }
    interval = 2.0 if abs(transform.a) <= 1.0 else 5.0
    contours = generate_contours(clean_elevation, transform, buffer_geometry, interval)
    return TerrainResult(
        elevation=elevation,
        slope_percent=slope_percent,
        slope_degrees=slope_degrees,
        aspect_degrees=aspect,
        hillshade=hillshade,
        transform=transform,
        crs=crs,
        parcel_mask=parcel_mask,
        buffer_mask=buffer_mask,
        metrics=metrics,
        contours=contours,
        coverage_fraction=coverage,
        warning=warning,
    )
