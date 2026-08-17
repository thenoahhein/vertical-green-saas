from __future__ import annotations

import json

import httpx
from shapely.geometry import box
from sitesense.ecology import EcologySourceError, run_ecology


class FakeEcologyClient:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self.payloads = payloads

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        parts = url.rstrip("/").split("/")
        layer_id = parts[-2] if parts[-1] == "query" else parts[-1]
        key = f"{layer_id}/query" if parts[-1] == "query" else layer_id
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, content=json.dumps(self.payloads[key]).encode())

    def close(self) -> None:
        pass


def _feature(name: str, code: str) -> dict[str, object]:
    return {
        "geometry": {
            "rings": [[
                [-97.0, 30.0], [-96.999, 30.0], [-96.999, 30.001], [-97.0, 30.001], [-97.0, 30.0],
            ]]
        },
        "properties": {"CommonName": name, "Veg_ID": code},
    }


def _metadata() -> dict[str, object]:
    return {"fields": [{"name": "CommonName"}, {"name": "Veg_ID"}]}


def test_ecology_selects_intersecting_layers_and_clips_percent() -> None:
    payloads = {"10": {"features": [_feature("Blackland prairie", "BP")]}}
    payloads.update({str(layer_id): {"features": []} for layer_id in (4, 5, 11)})
    payloads.update({str(layer_id): _metadata() for layer_id in (10, 4, 5, 11)})
    payloads["10/query"] = {"features": [_feature("Blackland prairie", "BP")]}
    payloads["4/query"] = {"features": []}
    payloads["5/query"] = {"features": []}
    payloads["11/query"] = {"features": []}
    result = run_ecology(box(-97.0, 30.0, -96.998, 30.001), 1.0, FakeEcologyClient(payloads))
    assert len(result.units) == 1
    assert result.units[0].system_vegetation_type == "Blackland prairie"
    assert result.units[0].source_classification_code == "BP"
    assert result.answered_layers == (10,)
    assert result.metrics["vegetation_type_acres"]["Blackland prairie"] > 0


def test_ecology_no_features_is_unavailable() -> None:
    payloads = {str(layer_id): _metadata() for layer_id in (10, 4, 5, 11)}
    payloads.update({f"{layer_id}/query": {"features": []} for layer_id in (10, 4, 5, 11)})
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


def test_ecology_multipart_rings_preserve_exteriors_and_holes() -> None:
    outer_one = [[-97.0, 30.0], [-96.999, 30.0], [-96.999, 30.001], [-97.0, 30.001], [-97.0, 30.0]]
    hole = [[-96.9998, 30.0002], [-96.9992, 30.0002], [-96.9992, 30.0008], [-96.9998, 30.0008], [-96.9998, 30.0002]]
    outer_two = [[-96.998, 30.0], [-96.997, 30.0], [-96.997, 30.001], [-96.998, 30.001], [-96.998, 30.0]]
    feature = {
        "geometry": {"rings": [outer_one, hole[::-1], outer_two]},
        "properties": {"CommonName": "Prairie", "Veg_ID": 1},
    }
    payloads = {str(layer_id): _metadata() for layer_id in (10, 4, 5, 11)}
    payloads.update({f"{layer_id}/query": {"features": []} for layer_id in (4, 5, 11)})
    payloads["10/query"] = {"features": [feature]}
    result = run_ecology(
        box(-97.0, 30.0, -96.997, 30.001),
        10.0,
        FakeEcologyClient(payloads),
    )
    assert result.units[0].geometry.geom_type == "MultiPolygon"
    assert result.units[0].geometry.area < 0.000003


def test_ecology_paginates_transfer_limit() -> None:
    payloads = {str(layer_id): _metadata() for layer_id in (10, 4, 5, 11)}
    payloads.update({f"{layer_id}/query": {"features": []} for layer_id in (4, 5, 11)})
    payloads["10/query"] = {
        "features": [_feature("Prairie", "1")],
        "exceededTransferLimit": True,
    }
    payloads["10/query?offset=1"] = {"features": [_feature("Woodland", "2")]}

    class PagingClient(FakeEcologyClient):
        def get(self, url: str, **kwargs: object) -> httpx.Response:
            params = kwargs.get("params", {})
            if url.endswith("/10/query") and isinstance(params, dict) and params.get("resultOffset") == "1":
                self.payloads["10/query"] = self.payloads["10/query?offset=1"]
            return super().get(url, **kwargs)

    result = run_ecology(
        box(-97.0, 30.0, -96.998, 30.001),
        1.0,
        PagingClient(payloads),
    )
    assert len(result.units) == 2


def test_ecology_keeps_answered_layers_when_one_layer_fails() -> None:
    payloads = {str(layer_id): _metadata() for layer_id in (10, 4, 5, 11)}
    payloads.update({f"{layer_id}/query": {"features": []} for layer_id in (4, 5, 11)})
    payloads["10/query"] = {"features": [_feature("Prairie", "1")]}
    payloads["4"] = {"fields": [{"name": "CommonName"}]}
    result = run_ecology(
        box(-97.0, 30.0, -96.998, 30.001),
        1.0,
        FakeEcologyClient(payloads),
    )
    assert result.answered_layers == (10,)
    assert result.warnings[0]["code"] == "ecology_partial_source"
    assert result.warnings[0]["failed_layer_ids"] == [4]
