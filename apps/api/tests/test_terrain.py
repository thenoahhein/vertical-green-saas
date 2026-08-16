from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import MultiPolygon, box
from sitesense.terrain import (
    NODATA,
    ONE_METER_DATASET,
    THIRD_ARC_SECOND_DATASET,
    TerrainProduct,
    TerrainSelection,
    analyze_elevation,
    cached_products_for_bounds,
    generate_contours,
    read_mosaic,
    select_products,
)


@dataclass
class FakeResponse:
    payload: dict[str, object]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeClient:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        dataset = str(kwargs["params"]["datasets"])  # type: ignore[index]
        return FakeResponse(self.responses[dataset])


class FlakyClient(FakeClient):
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        super().__init__(responses)
        self.calls = 0

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("catalog timeout")
        return super().get(_url, **kwargs)


def _item(dataset: str, bounds: tuple[float, float, float, float], url: str) -> dict[str, object]:
    return {
        "title": dataset,
        "downloadURL": url,
        "boundingBox": {
            "minX": bounds[0],
            "minY": bounds[1],
            "maxX": bounds[2],
            "maxY": bounds[3],
        },
    }


def test_source_selection_prefers_complete_one_meter_coverage() -> None:
    client = FakeClient(
        {
            ONE_METER_DATASET: {"items": [_item(ONE_METER_DATASET, (0, 0, 10, 10), "one.tif")]},
            THIRD_ARC_SECOND_DATASET: {"items": []},
        }
    )
    selection = select_products((1, 1, 9, 9), client)
    assert selection.used_fallback is False
    assert selection.products[0].source_url == "one.tif"


def test_source_selection_retries_transient_catalog_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FlakyClient(
        {
            ONE_METER_DATASET: {"items": [_item(ONE_METER_DATASET, (0, 0, 10, 10), "one.tif")]},
            THIRD_ARC_SECOND_DATASET: {"items": []},
        }
    )
    monkeypatch.setattr("sitesense.terrain.time.sleep", lambda _seconds: None)
    selection = select_products((1, 1, 9, 9), client)
    assert selection.products[0].source_url == "one.tif"
    assert client.calls == 2


def test_source_selection_falls_back_when_one_meter_is_incomplete() -> None:
    client = FakeClient(
        {
            ONE_METER_DATASET: {"items": [_item(ONE_METER_DATASET, (0, 0, 5, 10), "one.tif")]},
            THIRD_ARC_SECOND_DATASET: {
                "items": [_item(THIRD_ARC_SECOND_DATASET, (0, 0, 10, 10), "third.tif")]
            },
        }
    )
    selection = select_products((1, 1, 9, 9), client)
    assert selection == TerrainSelection(
        products=(selection.products[0],),
        used_fallback=True,
        warning="1 m 3DEP coverage did not fully cover the buffered parcel; used 1/3 arc-second DEM.",
    )
    assert selection.products[0].source_url == "third.tif"


def test_source_selection_reports_empty_coverage() -> None:
    client = FakeClient({ONE_METER_DATASET: {"items": []}, THIRD_ARC_SECOND_DATASET: {"items": []}})
    selection = select_products((1, 1, 9, 9), client)
    assert selection.products == ()
    assert selection.warning == "No 3DEP product fully covers the buffered parcel."


def test_cached_catalog_fallback_selects_covering_product() -> None:
    product = TerrainProduct(
        title="cached",
        dataset_name=ONE_METER_DATASET,
        source_url="s3://cached.tif",
        bounds=(0, 0, 10, 10),
        spatial_resolution="1 m",
        published_at=None,
        byte_size=None,
    )
    selection = cached_products_for_bounds((product,), (1, 1, 9, 9))
    assert selection.products == (product,)
    assert selection.used_fallback is True
    assert "cached" in (selection.warning or "")


def test_cached_catalog_fallback_reports_no_usable_product() -> None:
    selection = cached_products_for_bounds((), (1, 1, 9, 9))
    assert selection.products == ()
    assert "no cached" in (selection.warning or "").lower()


def test_known_gradient_has_analytic_slope_and_aspect() -> None:
    rows, columns = np.mgrid[:32, :32]
    elevation = (100 + columns * 0.1 + rows * 0.2).astype("float32")
    result = analyze_elevation(
        elevation,
        from_origin(0, 32, 1, 1),
        "EPSG:26914",
        box(4, 4, 28, 28),
        box(0, 0, 32, 32),
        24 * 24 / 4046.8564224,
    )
    expected_percent = np.hypot(0.1, 0.2) * 100
    assert np.nanmean(result.slope_percent[5:-5, 5:-5]) == pytest.approx(expected_percent, rel=0.03)
    assert result.metrics["slope_statistics_surface"] == "3x3 focal-mean-smoothed elevation"
    assert result.coverage_fraction == 1.0


def test_constant_surface_is_flat_and_has_full_coverage() -> None:
    elevation = np.full((24, 24), 150, dtype="float32")
    result = analyze_elevation(
        elevation,
        from_origin(0, 24, 1, 1),
        "EPSG:26914",
        box(2, 2, 22, 22),
        box(0, 0, 24, 24),
        400 / 4046.8564224,
    )
    assert np.nanmax(result.slope_percent) == pytest.approx(0)
    assert result.metrics["relief_m"] == pytest.approx(0)
    assert result.warning is None


def test_nodata_produces_typed_coverage_warning() -> None:
    elevation = np.full((24, 24), 150, dtype="float32")
    elevation[:, :12] = NODATA
    result = analyze_elevation(
        elevation,
        from_origin(0, 24, 1, 1),
        "EPSG:26914",
        box(2, 2, 22, 22),
        box(0, 0, 24, 24),
        400 / 4046.8564224,
    )
    assert result.warning is not None
    assert result.warning["code"] == "terrain_coverage_incomplete"
    assert result.warning["missing_fraction"] > 0


def test_zero_coverage_produces_source_unavailable_warning() -> None:
    elevation = np.full((24, 24), NODATA, dtype="float32")
    result = analyze_elevation(
        elevation,
        from_origin(0, 24, 1, 1),
        "EPSG:26914",
        box(2, 2, 22, 22),
        box(0, 0, 24, 24),
        400 / 4046.8564224,
    )
    assert result.coverage_fraction == 0
    assert result.warning == {
        "code": "terrain_source_unavailable",
        "message": "3DEP elevation coverage is unavailable for this parcel.",
        "missing_fraction": 1.0,
    }


def test_adjacent_fixture_tiles_mosaic_on_one_grid() -> None:
    from sitesense.terrain import TerrainProduct

    root = Path(__file__).parent / "fixtures" / "terrain"
    products = (
        TerrainProduct(
            "left", ONE_METER_DATASET, str(root / "seam_left.tif"), (500000, 0, 500016, 16), "1 m", None, None
        ),
        TerrainProduct(
            "right", ONE_METER_DATASET, str(root / "seam_right.tif"), (500016, 0, 500032, 16), "1 m", None, None
        ),
    )
    target_bounds = transform_bounds("EPSG:26914", "EPSG:4326", 500000, 0, 500032, 16)
    mosaic, _, _, contributors = read_mosaic(products, target_bounds, "EPSG:26914")
    assert mosaic.shape[0] >= 16
    assert mosaic.shape[1] >= 32
    assert np.all(mosaic[:16, :16] == 100)
    assert np.all(mosaic[:16, 16:] == 200)
    assert contributors == (str(root / "seam_left.tif"), str(root / "seam_right.tif"))


def test_overlapping_tiles_prefer_newest_publication_and_contributors() -> None:
    root = Path(__file__).parent / "fixtures" / "terrain"
    old = TerrainProduct(
        "old",
        ONE_METER_DATASET,
        str(root / "overlap_old.tif"),
        (500000, 0, 500016, 16),
        "1 m",
        datetime(2017, 1, 1, tzinfo=UTC),
        None,
    )
    new = TerrainProduct(
        "new",
        ONE_METER_DATASET,
        str(root / "overlap_new.tif"),
        (500000, 0, 500016, 16),
        "1 m",
        datetime(2018, 1, 1, tzinfo=UTC),
        None,
    )
    target_bounds = transform_bounds("EPSG:26914", "EPSG:4326", 500000, 0, 500016, 16)
    mosaic, _, _, contributors = read_mosaic((old, new), target_bounds, "EPSG:26914")
    assert np.all(mosaic[:16, :16] == 300)
    assert contributors == (str(root / "overlap_new.tif"),)


def test_aspect_matches_north_up_analytic_planes() -> None:
    rows, columns = np.mgrid[:32, :32]
    north_descending = (100 - (31 - rows) * 0.1).astype("float32")
    east_ascending = (100 + columns * 0.1).astype("float32")
    kwargs = {
        "transform": from_origin(0, 32, 1, 1),
        "crs": "EPSG:26914",
        "parcel_geometry": box(4, 4, 28, 28),
        "buffer_geometry": box(0, 0, 32, 32),
        "parcel_acres": 24 * 24 / 4046.8564224,
    }
    north_result = analyze_elevation(north_descending, **kwargs)
    east_result = analyze_elevation(east_ascending, **kwargs)
    assert np.nanmedian(north_result.aspect_degrees[5:-5, 5:-5]) == pytest.approx(0, abs=0.5)
    assert np.nanmedian(east_result.aspect_degrees[5:-5, 5:-5]) == pytest.approx(270, abs=0.5)


def test_contours_include_levels_index_flags_and_flatten_clipped_parts() -> None:
    rows, columns = np.mgrid[:32, :32]
    elevation = (99 + rows * 0.5).astype("float32")
    clip = MultiPolygon([box(8, 8, 24, 12), box(8, 20, 24, 24)])
    contours = generate_contours(elevation, from_origin(0, 32, 1, 1), clip, 2)
    assert contours
    levels_feet = [level * 3.280839895 for level, _, _ in contours]
    assert levels_feet == sorted(levels_feet)
    assert all(level % 2 == pytest.approx(0, abs=0.01) for level in levels_feet)
    assert any(is_index for _, is_index, _ in contours)
    assert all(geometry.geom_type in {"LineString", "MultiLineString"} for _, _, geometry in contours)


def test_bucket_acres_reconcile_to_parcel_area() -> None:
    elevation = np.full((40, 40), 150, dtype="float32")
    parcel = box(5, 5, 35, 35)
    expected_acres = parcel.area / 4046.8564224
    result = analyze_elevation(
        elevation,
        from_origin(0, 40, 1, 1),
        "EPSG:26914",
        parcel,
        box(0, 0, 40, 40),
        expected_acres,
    )
    bucket_acres = sum(float(bucket["acres"]) for bucket in result.metrics["slope_histogram"])
    assert bucket_acres == pytest.approx(expected_acres, rel=0.01)
