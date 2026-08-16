from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import box
from sitesense.hydrology import (
    HydrologySourceError,
    WhiteboxBinaryError,
    fetch_3dhp,
    fetch_wbd_membership,
    run_hydrology,
    whitebox_binary_path,
)


def _whitebox_available() -> bool:
    try:
        whitebox_binary_path()
    except WhiteboxBinaryError:
        return False
    return True


@dataclass
class Response:
    payload: dict[str, Any]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class Client:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self.payloads = payloads

    def get(self, url: str, **kwargs: Any) -> Response:
        if "/wbd/" in url:
            layer = url.split("/wbd/MapServer/", 1)[1].split("/", 1)[0]
            return Response(self.payloads[f"wbd-{layer}"])
        layer = url.rsplit("/", 2)[-2]
        return Response(self.payloads[f"3dhp-{layer}"])

    def close(self) -> None:
        return None


def test_3dhp_and_wbd_fixture_queries() -> None:
    payloads = {
        **{f"3dhp-{layer}": {"features": []} for layer in (20, 30, 40, 50, 60, 80)},
        "3dhp-50": {
            "features": [
                {
                    "attributes": {"featuretypelabel": "Channel Line"},
                    "geometry": {"paths": [[[-97.3, 30.1], [-97.31, 30.11]]]},
                }
            ]
        },
        "wbd-5": {"features": [{"attributes": {"huc10": "1209030102", "name": "Piney Creek-Colorado River"}}]},
        "wbd-6": {"features": [{"attributes": {"huc12": "120903010206", "name": "Copperas Creek-Colorado River"}}]},
    }
    client = Client(payloads)
    result = fetch_3dhp((-97.32, 30.1, -97.3, 30.12), client)
    assert result[50][0]["attributes"]["featuretypelabel"] == "Channel Line"
    membership = fetch_wbd_membership(-97.3119, 30.1101, client)
    assert membership["huc12"]["huc12"] == "120903010206"


def test_reference_service_outage_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenClient(Client):
        def get(self, url: str, **kwargs: Any) -> Response:
            raise OSError("offline")

    with pytest.raises(HydrologySourceError, match="failed"):
        fetch_3dhp((-97.32, 30.1, -97.3, 30.12), BrokenClient({}))


def test_missing_whitebox_binary_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sitesense.hydrology.whitebox_binary_path", lambda: (_ for _ in ()).throw(WhiteboxBinaryError("missing")))
    with pytest.raises(WhiteboxBinaryError, match="missing"):
        run_hydrology(
            np.ones((8, 8), dtype="float32"),
            from_origin(0, 8, 1, 1),
            "EPSG:26914",
            box(2, 2, 6, 6),
            1.0,
        )


@pytest.mark.skipif(
    not _whitebox_available(),
    reason="WhiteboxTools binary is not present in this test environment",
)
def test_v_valley_routes_to_one_coherent_corridor() -> None:
    columns = np.indices((64, 64))[1]
    elevation = np.abs(columns - 32).astype("float32")
    result = run_hydrology(
        elevation,
        from_origin(0, 64, 1, 1),
        "EPSG:26914",
        box(20, 20, 44, 44),
        1.0,
        stream_threshold_cells=2,
    )
    assert result.flow_accumulation[32, 0] <= result.flow_accumulation[32, -1]
    assert result.drainage_lines
    assert result.valleys


def test_whitebox_path_is_explicit() -> None:
    try:
        path = whitebox_binary_path()
    except WhiteboxBinaryError:
        pytest.skip("WhiteboxTools binary is not present in this test environment")
    assert path.name == "whitebox_tools"
