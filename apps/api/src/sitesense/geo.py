from collections.abc import Callable
from typing import cast

from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

TEXAS_AREA_CRS = "EPSG:6579"
TEXAS_LENGTH_CRS = "EPSG:3081"
_AREA_PROJECTOR = Transformer.from_crs("EPSG:4326", TEXAS_AREA_CRS, always_xy=True)
_LENGTH_PROJECTOR = Transformer.from_crs("EPSG:4326", TEXAS_LENGTH_CRS, always_xy=True)


def project_for_area(geometry: BaseGeometry) -> BaseGeometry:
    """Project WGS84 geometry to Texas Centric Albers equal-area metres."""
    projector: Callable[[float, float], tuple[float, float]] = _AREA_PROJECTOR.transform
    return transform(projector, geometry)


def project_for_length(geometry: BaseGeometry) -> BaseGeometry:
    """Project WGS84 geometry to a conformal metre CRS for line lengths."""
    projector: Callable[[float, float], tuple[float, float]] = _LENGTH_PROJECTOR.transform
    return transform(projector, geometry)


def acreage(geometry: BaseGeometry) -> float:
    """Return acres using the shared equal-area projected CRS."""
    return cast(float, project_for_area(geometry).area) / 4046.8564224


def length_meters(geometry: BaseGeometry) -> float:
    """Return metres using the shared conformal projected CRS."""
    return cast(float, project_for_length(geometry).length)
