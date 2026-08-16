# SiteSense architecture

The platform is layered so source volatility does not leak into client proposals:

1. **External source adapters** fetch parcel, terrain, hydrology, soils, ecology, flood, wells, and climate data.
2. **Normalized derived site model** stores a property and parcel plus analysis, layers, metrics, provenance, and confidence.
3. **Deterministic analysis** computes measurements in a Texas-appropriate projected CRS (central Texas acreage uses EPSG:6578/6579 or an equal-area equivalent), never raw WGS84 degrees.
4. **Rule engine** turns metrics into opportunities.
5. **Narrative** explains evidence, confidence, and limitations.
6. **Proposal** maps accepted opportunities to scope, pricebook items, and a client-facing PDF.

The derived site model tree is:

`property → parcel → site_analysis → {terrain, hydrology, soils, ecology, flood, groundwater, climate} → layers + metrics + opportunities → project_features → scope → proposal`.

Every derived result is confidence-scored with a machine-readable reason and can be linked to one or more `data_sources` using `analysis_source_refs`. Partial category results are represented by status rather than hidden nulls.

## Adding a data source

Implement an adapter under the source-adapter layer, register its endpoint and access method in `data_sources`, normalize its output into the relevant spatial/model table, and create `analysis_source_refs` rows for every derived record. Add deterministic fixture tests; unit tests must not call government services.

## Parcel source adapter seam

Parcel discovery uses a source-adapter boundary rather than placing ArcGIS or
geocoder details in API routes. An adapter accepts a WGS84 point and query
parameters, then returns normalized parcel records with the originating
`DataSource` URL and raw source attributes. `ArcGISParcelAdapter` implements
the four county layer-0 services; the Census geocoder only supplies a
coordinate and is never treated as an authoritative parcel source.

Adapters use bounded HTTP timeouts. Remote errors are represented as
unavailable health results and surfaced with machine-readable reasons; they do
not escape as uncaught route errors. Confirmed parcel rows create
`analysis_source_refs` records linking the organization, normalized table, and
registered county source.

The live query path is county-specific ArcGIS FeatureServer layer 0. The
statewide TxGIO StratMap parcel service is bulk-download-only and its public
`/query` capability is disabled, so it is not used for live parcel search.

### County parcel field mappings

These mappings were inspected from each service's `?f=json` metadata. The
services currently share the following field names; Caldwell uses lowercase
`block` for an otherwise unmapped field, which remains in raw attributes.

| County | Parcel ID | Situs address fields | Legal description | Appraisal acres | Owner/display name |
| --- | --- | --- | --- | --- | --- |
| Bastrop | `prop_id_text` | `situs_num`, `situs_street_prefx`, `situs_street`, `situs_street_sufix`, `situs_city`, `situs_state`, `situs_zip` | `legal_desc`, `legal_desc2`, `legal_desc3` | `legal_acreage` | `file_as_name` |
| Lee | `prop_id_text` | same as Bastrop | same as Bastrop | `legal_acreage` | `file_as_name` |
| Fayette | `prop_id_text` | same as Bastrop | same as Bastrop | `legal_acreage` | `file_as_name` |
| Caldwell | `prop_id_text` | same as Bastrop | same as Bastrop | `legal_acreage` | `file_as_name` |

Missing fields remain null. Unmapped fields are retained in
`raw_source_attributes`; computed acreage is calculated from normalized
geometry and is deliberately separate from appraisal-record acreage.

Parcel search uses a hybrid candidate strategy. The geocoded point drives a
paginated ArcGIS envelope query, while the parsed house number and normalized
situs street tokens drive a second attribute query. Results are merged by
county source feature ID before ranking. Exact situs matches rank ahead of
containing parcels, followed by true projected polygon distance; the API
returns only the first ten candidates after ranking. ArcGIS pages use each
service's configured `maxRecordCount` and continue while
`exceededTransferLimit` is set.

The Census geocoder returns a TIGER street-segment interpolation point, not a
rooftop or survey point. Attribute matching is therefore important when the
interpolated point falls outside the authoritative CAD parcel. Census
geographies resolve the county when possible; unsupported or unavailable
geographies fall back to querying all supported county adapters.
If Census returns no match, the same situs-only query runs across all
counties; only an empty result produces the typed address-not-found response.
Without a point, distance is null and candidates are centered from the
top-ranked parcel. Situs matches are only boosted within 5 km of a geocoded
point to prevent a distant same-number false match from outranking a
containing parcel.

Address resolution can be measured against the fixed benchmark with:

```bash
uv run python scripts/benchmark_address_resolution.py
```

The development compose stack bind-mounts the API and worker source trees.
Uvicorn reloads API edits, and `watchfiles` restarts the worker when Python
files change; source edits therefore take effect without rebuilding images.
The Dockerfiles remain self-contained for production-shaped image builds.

To add a county, inspect and record its layer metadata, add a
`CountySource`/`ParcelFieldMapping`, register its `DataSource` in `seed.py`,
and provide a response fixture before enabling the adapter in production.
Fixtures drive normal tests. `scripts/live_source_health.py` is an explicit
opt-in check for upstream availability and is excluded from CI.

TxGIO StratMap statewide parcels is bulk-download-only: its public MapServer `/query` capability is disabled. Launch-county parcel adapters use the verified ArcGIS FeatureServers (layer 0) for Bastrop, Lee, Fayette, and Caldwell. USGS 3DEP uses public `prd-tnm` staged COGs / TNMAccess; USDA Soil Data Access requires POST; TWDB, FEMA NFHL, and USFWS NWI are registered as ArcGIS services.

## Terrain analysis

Terrain is the first payload of the asynchronous analysis pipeline. A confirmed
parcel analysis queries TNMAccess for products intersecting the parcel plus a
configurable 500-meter buffer, prefers complete 1-meter 3DEP coverage, and
falls back to 1/3 arc-second DEM coverage when required. Product metadata is
stored in `data_sources`; every raster or contour layer records
`analysis_source_refs` back to the selected product rows.

Derivatives run on a projected metre grid after all intersecting COG windows
have been mosaicked. NumPy implements Horn 3x3 slope, aspect, and hillshade
derivatives. Raw elevation remains the raster basis, while reported slope
statistics use a 3x3 focal-mean-smoothed elevation surface to produce a
planning-grade contractor metric. Elevations are stored in metres and payloads
also expose contractor-facing feet values. Contours use matplotlib, a 2-foot
interval at 1-meter source resolution (5 feet otherwise), and are stored as
4326 geometries with 10-foot index metadata.

Terrain rasters are written as COGs to the S3-compatible object store under
organization/project/analysis-scoped keys. Coverage below 99 percent produces
a typed warning naming the missing fraction; zero valid parcel coverage
produces a typed source-unavailable warning while the job remains partial.

Hydrologically conditioned terrain products are intentionally not part of this
milestone. Local depressions, ridgelines, valleys, and terrain-derived drainage
networks require flow routing and belong to Milestone 3.
