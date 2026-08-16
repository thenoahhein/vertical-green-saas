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
planning-grade contractor metric. The slope histogram reports acreage from
parcel pixels and percentages of valid parcel slope pixels; the payload names
that denominator explicitly. Elevations are stored in metres and payloads also
expose contractor-facing feet values. Contours use matplotlib, a 2-foot
interval at 1-meter source resolution (5 feet otherwise), and are stored as
per-level 4326 geometries with elevation-in-feet and 10-foot index metadata.

Terrain rasters are written as COGs to the S3-compatible object store under
organization/project/analysis-scoped keys. Coverage below 99 percent produces
a typed warning naming the missing fraction; zero valid parcel coverage
produces a typed source-unavailable warning while the job remains partial.

Hydrologically conditioned terrain products are intentionally not part of this
milestone. Local depressions, ridgelines, valleys, and terrain-derived drainage
networks require flow routing and belong to Milestone 3.

## Hydrology analysis

Milestone 3 extends the terrain job with WhiteboxTools 2.3.6. The API and
worker images warm the MIT-licensed executable during image build; request
processing fails with a typed configuration error if the executable is absent
and never downloads it on demand. The measured Whitebox workflow was 0.64
seconds and approximately 69 MB for a 1,402-by-1,402 window, versus 1.14
seconds and approximately 103 MB for a 1,900-by-1,900 window. PySheds was
approximately ten times slower and five times more memory-intensive in the
same probe, and its accumulation path currently calls the removed NumPy 2.5
`in1d` symbol. RichDEM does not build on Python 3.12. A straightforward
Python priority-flood implementation alone took approximately 32 seconds on
2,000-by-2,000 cells, so it is not used in the request path.

Hydrology rasters are staged in a cleaned per-job directory because
WhiteboxTools uses file paths: conditioned DEM, D8 pointer, D8 accumulation,
stream raster, and local subbasins are persisted as COGs or vector layers.
Drainage extraction uses an explicit accumulation threshold recorded in layer
metadata. Depressions, ridgelines, valleys, catchments, and corridor
statistics are local-window products and are not authoritative watershed
delineations.

The D8 accumulation operation is explicitly invoked with Whitebox's `pntr`
flag because its input is the D8 pointer raster, not an elevation raster.
Accumulation output is therefore measured in cells (`out_type=cells`). The
default stream threshold is 100 cells for the 1-meter DEM workflow.

Hydrology products are filtered rather than dissolved into a single geometry.
The named defaults for 1-meter lidar are:

- depressions: minimum 9 m² area and 0.3 m maximum fill depth;
- local catchments: minimum 100 m² area;
- ridgelines and major valleys: minimum 1 m centerline length (one DEM cell).

Each retained depression is persisted separately with fill depth and an
estimated filled volume. Each retained catchment remains a partition feature,
and each retained ridge/valley remains a centerline feature. Applied minimums
are recorded in layer metadata, so omitted small features are distinguishable
from missing analysis output. Raster masks for ridges and valleys are traced
through cell centers and filtered by length; polygon boundaries are never used
as their line representation.

Corridor contributing acreage is calculated independently for each extracted
drainage corridor from the maximum accumulation cells along that corridor.
Parcel intersection is reported as corridor length inside the parcel, in
contractor-facing feet with the metric length retained in layer metadata.
Mapped-water relationships are classified against 3DHP flowlines and
waterbodies within a 30-meter tolerance; an unavailable 3DHP query is reported
as unavailable rather than as an absence of mapped hydrography. Whitebox
stream vectorization is read from its Shapefile output when present. The
fallback traces active raster cells through their centers, avoiding the
doubled outlines produced by polygon-mask boundaries.

The actual warmed WhiteboxTools executable version is recorded with each
hydrology raster layer, independently of the Python wrapper version, so
algorithm provenance remains traceable.

Contributing acreage is always labeled `within analysis window`. Boundary
inflow is detected from accumulation values at the analysis-window edge. When
significant inflow is present, the value is a lower bound and the payload
emits `hydrology_window_truncated`; one bounded expansion from the default
500-meter buffer to 2 kilometers is attempted only for a real accumulation
inflow signal. Analysis grids are capped at 9,000,000 cells so the expansion
cannot silently allocate an unbounded raster; oversized requests degrade with
a typed terrain-source error. The system never publishes a bare parcel-scale
watershed acreage from a local window. WBD HUC10/HUC12
membership is regional context only, and the absence of a local 3DHP
Catchment feature is recorded as unavailable rather than treated as proof of
no upstream watershed.

All raster products use one nodata validity rule: a cell must be finite and
different from the declared nodata value within a numeric tolerance. This
rule is applied to terrain metrics, hydrology inputs, depression attributes,
and COG output. The float32 sentinel is not valid elevation data merely
because it passes `isfinite`.

Hydrology expansion routing uses a 5 m grid while the local parcel window
remains at source resolution. The routing resolution is recorded in metrics
and layer metadata; coarse expansion rasters are not parcel-grade products.
To bound pathological vectorization, at most 5,000 polygons and 5,000
line features are retained per product family, ranked by area or length.
When a cap applies, the analysis emits `hydrology_products_capped` rather
than silently presenting an incomplete product set.

Analysis job status records stage timings for source selection, mosaic reads,
terrain derivatives, Whitebox routing, vectorization/filtering, reference
queries, and persistence. The same timing data is included in persisted raster
layer metadata where applicable.

USGS 3DHP flowlines, waterbodies, and hydrolocations are stored as reference
layers with independent provenance. Terrain-derived depressions and water
features are labeled **potential water-management investigation areas** and
require contractor review; the analysis does not recommend building a pond or
other feature.
