---
name: testing-parcel-workspace
description: How to run and browser-test the SiteSense parcel workspace (address search → county CAD candidates → parcel confirmation) against the local docker compose stack.
---

# Testing the SiteSense parcel workspace

## Bring up / reuse the stack
- `docker compose -f infra/compose.yml up -d --build` (services: api :8000 with routes under `/api`,
  worker, web :3000, postgis :5432, redis, minio). api/worker source is bind-mounted with reload;
  `web` is a baked image running `npm run dev`, so frontend code changes need
  `docker compose -f infra/compose.yml up -d --build web`.
- Health endpoint is `GET /healthz` (NOT `/api/health`).
- Auth is a dev bearer token: `Authorization: Bearer dev-token` (see `apps/api/src/sitesense/config.py`).
- Seeded org id `00000000-0000-0000-0000-000000000001`; list projects with
  `curl -H "Authorization: Bearer dev-token" localhost:8000/api/projects`.

## Check these first when the browser can't talk to the API
- **CORS** is an explicit allow-list from `CORS_ALLOWED_ORIGINS` (set for the web origins in
  `infra/compose.yml`), not `*`. A browser-only `Failed to fetch` while `curl` works means the
  origin isn't allowed; verify the preflight with
  `curl -i -X OPTIONS -H "Origin: http://localhost:3000" -H "Access-Control-Request-Method: GET" localhost:8000/api/parcel-search`
  (expect 200 + `Access-Control-Allow-Origin`). Serving the UI from a different port/host needs that
  origin added to the env var.
- **Projects**: the workspace creates its own project via `POST /api/projects` per confirmation flow;
  `NEXT_PUBLIC_PROJECT_ID` only overrides it. A project holds exactly one property — confirming a
  *different* parcel into a project that already has one returns a typed 409
  `project_already_has_property` (re-confirming the same parcel is idempotent).

## UI paths (apps/web/components/PropertyWorkspace.tsx)
- Single page at `/`: search input + "Search parcels", candidate `<ul>`, a `Confirm parcel <id>`
  button below the list, "Layers" checkboxes, MapLibre map on the right.
- Hover is **preview only** (highlights on the map); committed selection needs a click or Enter/Space
  on the row, and the confirm button names the parcel it will submit — read that label to know what
  will be persisted, then re-verify the row in PostGIS.
- `npm test` (vitest + React Testing Library, `apps/web/components/PropertyWorkspace.test.tsx`) covers
  the hover-must-not-change-selection regression; run it before browser testing selection changes.
- Verify persistence directly:
  `docker exec infra-postgis-1 psql -U sitesense -d sitesense -c "select p.appraisal_parcel_id, p.situs_address, p.computed_acres from parcels p join properties pr on pr.id=p.property_id where pr.project_id='<project uuid>' order by p.created_at;"`
- Downstream endpoints (terrain/hydrology/soils/opportunities/proposal) intentionally return HTTP 501.

## Reliable test addresses (hit live county CAD + US Census, ~1-2 s each)
- `1311 Chestnut St, Bastrop, TX 78602` → Bastrop parcel 35585 first
- `1385 CR 208, Giddings, TX 78942` → Lee 10014 first
- `18360 FM 86, Red Rock, TX 78662` → Caldwell 10759 first
- `4664 PIN OAK BRANCH RD, La Grange, TX 78945` → Census no-match; situs-only fallback returns Fayette 103123
- `zzzz nonexistent street, TX` → typed 404 `address_not_found`
- Basemap comes from `NEXT_PUBLIC_BASEMAP_STYLE_URL` (default `https://tiles.openfreemap.org/styles/liberty`);
  outbound internet to openfreemap.org and the county FeatureServers is required.

## Devin Secrets Needed
None — dev auth is the static `dev-token`; all upstream data sources are public.
