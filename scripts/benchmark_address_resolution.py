"""Opt-in live PRD §39 address-resolution benchmark."""

import asyncio
import json
from pathlib import Path

from shapely.geometry import Point
from sitesense.geocoding import GeocoderNoMatch, geocode, resolve_county
from sitesense.parcel_sources import search_counties

CASES = Path(__file__).parents[1] / "apps" / "api" / "tests" / "fixtures" / "benchmark" / "address_resolution.json"


async def run() -> None:
    cases = json.loads(CASES.read_text())["cases"]
    top1 = top3 = found = 0
    for case in cases:
        error: str | None = None
        try:
            try:
                geocoded = await geocode(case["address"])
            except GeocoderNoMatch:
                parcels = await search_counties(None, address=case["address"], buffer_meters=0)
            else:
                county = await resolve_county(case["address"])
                parcels = await search_counties(
                    Point(geocoded.longitude, geocoded.latitude),
                    county,
                    address=case["address"],
                )
            ids = [parcel.parcel_id for parcel in parcels]
            rank = ids.index(case["expected_parcel_id"]) + 1 if case["expected_parcel_id"] in ids else None
        except Exception as exc:
            rank = None
            ids = []
            error = f"{type(exc).__name__}: {exc}"
        if rank is not None:
            found += 1
            top1 += rank == 1
            top3 += rank <= 3
        suffix = f" error={error}" if error else ""
        print(f"{case['county']} {case['acreage_band']} rank={rank} n={len(ids)} {case['address']}{suffix}")
    total = len(cases)
    print(f"top1={top1}/{total} top3={top3}/{total} found={found}/{total}")


if __name__ == "__main__":
    asyncio.run(run())
