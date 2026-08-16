from __future__ import annotations

import json

import httpx
from shapely.geometry import box
from sitesense.ecology import EcologySourceError, run_ecology


class FakeEcologyClient:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self.payloads = payloads

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        layer_id = url.split("/")[-2]
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, content=json.dumps(self.payloads[layer_id]).encode())

    def close(self) -> None:
        pass


def _feature(name: str, code: str) -> dict[str, object]:
    return {
        "geometry": {
            "rings": [[
                [-97.0, 30.0], [-96.999, 30.0], [-96.999, 30.001], [-97.0, 30.001], [-97.0, 30.0],
            ]]
        },
        "attributes": {"SYSTEM_VEGETATION_TYPE": name, "CLASSIFICATION_CODE": code},
    }


def test_ecology_selects_intersecting_layers_and_clips_percent() -> None:
    payloads = {"10": {"features": [_feature("Blackland prairie", "BP")]} }
    payloads.update({"4": {"features": []}, "5": {"features": []}, "11": {"features": []}})
    result = run_ecology(box(-97.0, 30.0, -96.998, 30.001), 1.0, FakeEcologyClient(payloads))
    assert len(result.units) == 1
    assert result.units[0].system_vegetation_type == "Blackland prairie"
    assert result.units[0].source_classification_code == "BP"
    assert result.answered_layers == (10,)
    assert result.metrics["vegetation_type_acres"]["Blackland prairie"] > 0


def test_ecology_no_features_is_unavailable() -> None:
    payloads = {str(layer_id): {"features": []} for layer_id in (10, 4, 5, 11)}
    result = run_ecology(box(-97.0, 30.0, -96.998, 30.001), 1.0, FakeEcologyClient(payloads))
    assert result.units == []
    assert result.warnings[0]["code"] == "ecology_source_unavailable"


def test_ecology_upstream_failure_is_typed() -> None:
    class BrokenClient:
        def get(self, _url: str, **_kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("offline")

        def close(self) -> None:
            pass

    try:
        run_ecology(box(-97.0, 30.0, -96.998, 30.001), 1.0, BrokenClient())  # type: ignore[arg-type]
    except EcologySourceError as exc:
        assert "TPWD layer" in str(exc)
    else:
        raise AssertionError("expected typed ecology source error")
