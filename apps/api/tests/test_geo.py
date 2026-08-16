import pytest
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import transform
from sitesense.geo import acreage, length_meters, project_for_area, project_for_length


def test_area_round_trip_preserves_acreage() -> None:
    to_projected = Transformer.from_crs("EPSG:4326", "EPSG:6579", always_xy=True).transform
    center_x, center_y = to_projected(-97.3, 30.1)
    source = Polygon([(center_x, center_y), (center_x + 100, center_y), (center_x + 100, center_y + 40.468564224), (center_x, center_y + 40.468564224)])
    to_wgs84 = Transformer.from_crs("EPSG:6579", "EPSG:4326", always_xy=True).transform
    geographic = transform(to_wgs84, source)
    assert abs(acreage(geographic) - 1.0) < 0.001
    assert project_for_area(geographic).area == pytest.approx(source.area, rel=0.001)


def test_bastrop_square_is_one_acre() -> None:
    to_projected = Transformer.from_crs("EPSG:4326", "EPSG:6579", always_xy=True).transform
    center_x, center_y = to_projected(-97.3, 30.1)
    to_wgs84 = Transformer.from_crs("EPSG:6579", "EPSG:4326", always_xy=True).transform
    side = 4046.8564224**0.5
    source = Polygon([(center_x, center_y), (center_x + side, center_y), (center_x + side, center_y + side), (center_x, center_y + side)])
    assert acreage(transform(to_wgs84, source)) == pytest.approx(1.0, abs=0.01)


def test_length_uses_metre_projection() -> None:
    line = Polygon([(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001)]).boundary
    assert length_meters(line) > 400
    assert project_for_length(line).length == pytest.approx(length_meters(line))
