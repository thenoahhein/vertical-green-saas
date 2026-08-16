import asyncio

from sitesense.geocoding import resolve_county


def test_geographies_resolves_county_and_falls_back_on_failure() -> None:
    class Response:
        def __init__(self, payload: dict[str, object], error: Exception | None = None) -> None:
            self.payload = payload
            self.error = error

        def raise_for_status(self) -> None:
            if self.error:
                raise self.error

        def json(self) -> dict[str, object]:
            return self.payload

    class Client:
        def __init__(self, response: Response) -> None:
            self.response = response

        async def get(self, url: str, params: dict[str, str]) -> Response:
            return self.response

    resolved = asyncio.run(
        resolve_county(
            "123 Main St, Bastrop, TX",
            Client(Response({"result": {"addressMatches": [{"geographies": {"Counties": [{"BASENAME": "Bastrop"}]}}]}})),
        )
    )
    failed = asyncio.run(resolve_county("123 Main St", Client(Response({}, RuntimeError("upstream")))))
    assert resolved == "Bastrop"
    assert failed is None
