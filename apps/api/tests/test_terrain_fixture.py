from pathlib import Path

import rasterio


def test_terrain_fixture_opens_with_rasterio() -> None:
    fixture = Path(__file__).parent / "fixtures" / "terrain" / "bastrop_35585_subset.tif"
    with rasterio.open(fixture) as dataset:
        assert dataset.crs.to_string() == "EPSG:26914"
        assert dataset.count == 1
        assert dataset.width == 96
        assert dataset.height == 96
        assert dataset.nodata == -3.4028230607370965e38
