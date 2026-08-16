"""Opt-in live PRD §47 terrain benchmark over the committed address cases."""

import asyncio
import json
import time
from pathlib import Path

import httpx

CASES = Path(__file__).parents[1] / "apps" / "api" / "tests" / "fixtures" / "benchmark" / "address_resolution.json"
REQUIRED_FIELDS = {
    "elevation_min_m",
    "elevation_max_m",
    "elevation_mean_m",
    "relief_m",
    "mean_slope_percent",
    "slope_histogram",
}


async def run() -> None:
    cases = json.loads(CASES.read_text())["cases"]
    headers = {"Authorization": "Bearer dev-token"}
    async with httpx.AsyncClient(base_url="http://localhost:8000", headers=headers, timeout=180) as client:
        for case in cases:
            started = time.perf_counter()
            search = await client.get("/api/parcel-search", params={"address": case["address"]})
            search.raise_for_status()
            candidates = search.json()["candidates"]
            candidate = next(item for item in candidates if item["parcel_id"] == case["expected_parcel_id"])
            project = await client.post("/api/projects", json={"name": case["address"]})
            project.raise_for_status()
            project_id = project.json()["id"]
            confirm = await client.post(f"/api/projects/{project_id}/parcel", json={"candidate": candidate})
            confirm.raise_for_status()
            job = await client.post(f"/api/projects/{project_id}/analyze")
            job.raise_for_status()
            while True:
                status = await client.get(f"/api/projects/{project_id}/analysis/status")
                status.raise_for_status()
                if status.json()["stage"] in {"complete", "partial", "failed"}:
                    break
                await asyncio.sleep(1)
            analysis = await client.get(f"/api/projects/{project_id}/analysis")
            analysis.raise_for_status()
            terrain = analysis.json().get("terrain") or {}
            elapsed = time.perf_counter() - started
            missing = sorted(REQUIRED_FIELDS - terrain.keys())
            print(
                f"{case['county']} {case['expected_parcel_id']} "
                f"seconds={elapsed:.2f} coverage={terrain.get('coverage_fraction')} "
                f"all_required={not missing} missing={','.join(missing)}"
            )


if __name__ == "__main__":
    asyncio.run(run())
