# CLAUDE.md — desakota

Notebook-first spatial data analysis (ESDA) of urban sprawl in Vietnam's Red
River Delta. Hypothesis: building along national highways produces McGee-style
*desakota* (scattered, mixed town-and-farm growth), not compact cities.

## Quick orientation

- **Pipeline:** `01_preparation` (GADM communes + Geofabrik roads → QL18
  corridor, then 10 m LULC clipped to it → `processed/communes_ql18_corridor.gpkg`,
  the study area with `built_frac`, plus `processed/highways_ql18.gpkg`) →
  `02_esda` (commune Moran's I / LISA on the corridor; superseded as a method,
  kept as a baseline) → `03_hexagons` (H3 res 8/9/10 over the 2 km band, on land;
  `built_frac` per hex, distance decay, Moran's I / LISA →
  `processed/hexes_*_h3r{8,9,10}.gpkg`). Nothing is masked yet (road surface and
  urban wards are in, by decision 2026-10-18) → `04_corridor_width` (H3 res 8,
  2/3/5/8 km bands; patches = LISA High-High or `built_frac >= 0.5`, shape from
  `desakota.patches`: alpha shape + equivalent rectangle). Threshold patches
  reaching the band edge fall 97% → 54% of area from 2 to 8 km and haven't
  converged: the Bắc Ninh–Đông Triều built fabric is wider than 16 km. The
  equivalent rectangle scores *branching* patches as very elongated (up to 17),
  so elongation alone can't tell one ribbon from a web of strips. Relating patches
  to the road (`add_road_measures`: QL18 length lined, share of area within 1 km)
  does: ribbon-candidate area settles at 62 km² from a 5 km band on (it was
  298 km² at 2 km, where truncated webs looked like ribbons); most delta patches
  line long stretches of QL18 but lie mostly further away (road-touching webs).
  → `05_frontage` (per 100 m station of QL18 and side: built share of the
  frontage 30–100 m vs the background 500–1500 m; `desakota.frontage`,
  `landcover.compute_frontage_counts` →
  `processed/frontage_built_frac_station100m.gpkg`).
- **Notebooks 02–05 are successive approaches.** Keep superseded ones; a new
  approach gets the next number and a row in README "Attempts so far".
  `03_highways` was folded into 01 on 2026-10-17.
- **Setup and data download:** README "Quick start". Only one input is not in
  git: the 1.4 GB Geofabrik extract.
- **Checks before calling work done:** `uv run ruff format .` ·
  `uv run ruff check --fix .` · `uv run pyright` · `uv run pytest`.
- **Old name:** the project used to be `ntl_analysis` (nighttime lights). Any
  `ntl_analysis` / `NTL_*` you see is stale. The package is `desakota/` and the
  env var is `DESAKOTA_DATA_DIR`. The checkout folder may still carry the old name.

## Current phase: detect first, aggregate second — scope boundary

**Why the approach changed.** The first pass computed built-up % per commune
and ran Moran's I / LISA on it. Result: global Moran's I = 0.75, but the
significant High-High clusters were urban cores (Hà Nội, Hải Phòng: compact
blobs, 61% of HH communes >= 95% built-up), not corridors. A highway ribbon a
few hundred metres wide is diluted to nothing inside a ~2.4 km commune, and
built-up *density* finds cities, whereas desakota is defined by *mixing*. The
replacement follows Verbeek, Boussauw & Pisman (2014), *Landscape and Urban
Planning* 128:48-59 (doi:10.1016/j.landurbplan.2014.04.022): detect the
linear objects first, then aggregate a **length density (m/km²)**. The
commune-level ESDA (`02_esda`) is superseded as a method; it now runs on the
corridor only as a baseline, so don't build on it.

**In scope right now:**
- **Study area: the QL18 corridor**, not the whole delta (decided 2026-10-11).
  It is the old national road (`tier == 'national'`, selected with
  `compute_is_on_route(highways, 'QL18')`) from Bắc Ninh city through Chí Linh
  and all of Quảng Ninh to Móng Cái (Quảng Ninh included 2026-10-17, so the AOI
  is no longer limited to the 9 RRD provinces; communes join by distance,
  whatever their province). The AOI is the communes within `CORRIDOR_BUFFER_M`
  (2 km) of it, saved as `processed/communes_ql18_corridor.gpkg` with
  `dist_to_ql18_m`, `is_crossed_by_ql18` and `built_frac`; built by
  `notebooks/01_preparation.ipynb`. The co-signed expressway sections
  (Nội Bài–Bắc Ninh, "CT.09;CT.38;QL.18") have no frontage, so they are
  context, not corridor. LULC comes from tile 48Q only: the corridor ends at
  108.05°E, past zone 48's nominal edge, but inside 48Q's footprint (to 108.16°E).
- OSM road centrelines (`desakota.highways`), tiered by `ref` into
  expressway / national / provincial / other.
- Ribbon detection on the built-up signal: buffer, cluster, keep elongated
  clusters (Verbeek's reference thresholds: 25 m buffer, >= 200 m long,
  >= 80% of the adjoining road segment; recalibrate for 10 m pixels), with
  urban cores masked out first.
- Then ESDA on the *aggregated ribbon density*: Moran's I / LISA on a regular
  grid (several cell sizes, so MAUP is an explicit sensitivity check), plus
  ribbon-vs-distance-to-highway comparisons.

**Explicitly deferred — do not pull in without being asked:**
- Transit/public-transportation accessibility (isochrones, GTFS, Hansen-style
  accessibility indices). Note Moonen et al. (2014, cited by Verbeek) found
  Flemish ribbon homes *better* served than isolated dwellings, so if this
  returns the comparison group must be scattered development, not "nothing".
- Routing graphs (`osmnx.graph_from_*`, OSRM). Centrelines only.
- VIIRS DNB nighttime-lights ingestion (the original `ntl_analysis` focus).
- Building footprints (Microsoft `ms-buildings`): the closer analogue to
  Verbeek's cadastral data, but heavier. Revisit if 10 m LULC proves too
  coarse to resolve ribbons.

This boundary exists so dependencies and notebook complexity track what's
actually being analyzed. Revise this section when the phase changes, rather
than letting scope drift silently.

## Working convention: visualize every step, not just the end

This is a learning project — the point is to see what each transform does,
not just get a final figure. In notebooks:

- After loading, reprojecting, joining, or computing any new spatial variable,
  plot it before moving to the next cell — a quick `.plot()` / `.explore()` is
  enough, it doesn't need to be polished.
- Prefer a fast, cheap visualization at each step over one polished figure at
  the end. Polish only the figures that end up explaining a result.
- For spatial statistics specifically: every Moran's I / LISA computation
  should be paired with its plot (Moran scatterplot, cluster map) via
  `splot` — the number alone doesn't build intuition, and this is exactly
  what the ESDA tooling below is for.

## Tech stack

- Python 3.14 (`uv`, see `.python-version`)
- Vector / ESDA core: `geopandas`, `shapely` (2.x), `pyproj`, `pyogrio` (I/O
  engine), `libpysal` (spatial weights), `esda` (Moran's I, LISA, Geary's C,
  Getis-Ord), `splot` (ESDA visualization), `mapclassify` (choropleth
  classification schemes — Jenks, quantiles, etc.)
- Visualization: `matplotlib`, `folium` (interactive)
- Raster I/O: `rasterio` — windowed/clipped reads of the 10m LULC
  Cloud-Optimized GeoTIFFs (each STAC item is a full UTM-supercell mosaic,
  not an AOI-sized file, so a windowed read via `rasterio.windows.from_bounds`
  is required, not optional). Note: rasterio's type-stub coverage for its
  C-extension-backed classes (`DatasetReader` etc.) is incomplete — Pylance
  will show `Unknown` for parts of its interface. Don't fight this
  project-wide; wrap rasterio calls at a narrow, explicitly-typed boundary
  function in `desakota/` that hands back plain `numpy`/`geopandas` types,
  per the strict-typing convention below.
- OSM: `osmnx` 2.x, used only through `features_from_polygon` (raw ways with
  original tags), never as a routing graph — a graph splits ways at junctions
  and duplicates two-way edges, which double-counts length and loses `ref`.
  Since 2026-10 the public Overpass endpoint refuses scripted clients, so roads
  are read offline from a dated Geofabrik extract
  (`raw/osm/geofabrik_vietnam-261016/`, OSM as of 2026-10-16) with
  `load_highways`; `fetch_highways` only works against a private Overpass.
  Overpass speaks WGS 84 lon/lat only; `desakota.highways` handles the
  reprojection in and out.
- Hexagons: `h3` (h3-py 4.x, untyped: wrap calls with a narrow pyright ignore).
  Its boundaries come back as (lat, lng); `desakota.hexgrid` swaps them. Run
  `esda.Moran_Local` with `n_jobs=1`: parallel workers each copy the data and
  ran this 7 GB WSL out of memory at ~70k hexes (single-process takes ~10 s).
- Also installed: `rasterstats`, `networkx` (chainage axis in `desakota.frontage`).
- dev: jupyterlab, ipykernel, pytest, ruff, pyright (strict), `pandas-stubs`,
  `types-shapely`. With the stubs, the remaining pyright noise is upstream
  `**kwargs` left untyped in geopandas/shapely signatures (`to_file`,
  `read_file`, `set_precision`). Suppress those with a narrow
  `# pyright: ignore[reportUnknownMemberType] -- <reason>`, never file-wide.

**Runs in WSL (Ubuntu 24.04) or Linux.** `uv` is in `~/.local/bin`, which may
not be on `PATH` (prefix commands with `export PATH="$HOME/.local/bin:$PATH"`).
Never create or run the `.venv` from Windows; the layouts are incompatible.

If the checkout is under `/mnt/c`, installs are slow but not hung: uv can't
hardlink from its ext4 cache across 9P, so it copies every file (measured:
5 m 38 s for 2 packages). Run long `uv sync` / `uv add` calls in the
background. On this machine `.venv` is a symlink to `~/.venvs/desakota` on
ext4 (sync in about 3 s); don't replace it with a real directory. Nothing loads
`.env`; env vars must be exported in the shell.

## Conventions

- **Notebooks are exploratory.** Once a transform is worth keeping, move it
  into `desakota/` so ruff, pyright, and pytest see it, then import it back.
- **Notebooks are numbered** in the order they are meant to be read.
- **Never hardcode paths.** Use `DataLayout.from_env()`; the root is
  overridable via `DESAKOTA_DATA_DIR`.
- **`data/raw/` is read-only to code, except for fetchers.** A fetcher
  (`desakota.highways.fetch_highways`, the LULC window clip) writes a result
  once, keyed by its query, with a JSON sidecar recording when/how it was
  pulled, and only refetches on `force=True`. External sources like OSM change
  daily, so a raw file cannot be regenerated from code alone — treat it as
  data, not build output. `interim/` and `processed/` must be rebuildable from
  `raw/` alone.
- **Git tracks only the raw inputs the notebooks read.** `.gitignore` lists
  them one by one under `data/raw/`; `interim/` and `processed/` are never
  committed. A notebook that starts reading a new raw file must add that file
  to the list, and the README "Data in git" table, in the same change. Files
  over ~50 MB (the Geofabrik `.gpkg`) stay out and get a download step in the
  README instead, with only their `.json` sidecar committed. Don't commit
  legacy or practice data sitting in `raw/`.
- **Fetchers are cached and tested with the network stubbed.** Unit tests
  must never hit Overpass/STAC; verify against the live service manually.
- Ruff lints `.ipynb` too (`extend-include`). Notebooks are exempt from T20
  (print), E402 (imports after a setup cell), and B018 (bare trailing
  expression).

## Geospatial conventions

- **Always state the CRS**, in code and in prose. Silent reprojection is a
  bug.
- **Never have a single ambiguous `CRS` constant.** This project has (at
  least) three CRSs in legitimate concurrent use — WGS84 for STAC bbox search
  params and GADM's native format, UTM zone 48N for the raster/analysis grid,
  EPSG:6933 for area math — and collapsing them into one constant named
  `CRS` is how a GeoDataFrame ends up silently left in the wrong one with
  nothing in the code flagging it. Use distinct names: `CRS_WGS84`,
  `CRS_UTM`, `CRS_EQUAL_AREA` (or equivalent), never a lone `CRS`. They live
  in `desakota.crs`; import them, don't retype EPSG strings.
- **Pick one canonical working CRS per pipeline stage, reproject only at
  named boundaries.** The working CRS here is **EPSG:32648** — it's the LULC
  raster's native grid, so vector layers should be reprojected into it once,
  immediately after load, and stay there as their default state. Every
  deviation (WGS84 for a STAC `bbox` param or folium display, EPSG:6933 for
  area/fragmentation math) should be one clearly-commented reprojection
  justified by what that specific downstream tool requires — not an ad hoc
  `.to_crs()` inside whatever cell happens to need coordinates at that
  moment. `.to_crs()` returns a new object; always rebind the result to the
  variable that becomes canonical going forward, never let a reprojected
  copy be computed once and discarded.
- **A vector/raster CRS mismatch fails silently, not loudly.** `rasterstats`
  and `rasterio` do not reproject for you and do not raise on mismatch —
  they just find no overlapping pixels and return empty results. Assert the
  match explicitly at every vector-meets-raster boundary
  (`assert vector.crs == raster_crs`, or a reproject-if-needed guard) rather
  than discovering the mismatch several cells later via suspiciously-empty
  output.
- 10m annual land-use/land-cover (current primary source, via Planetary
  Computer STAC): UTM zone matching the tile (RRD mostly falls in
  **EPSG:32648**), coordinate order **(lon, lat)** before projection.
- Anything area-weighted (density, fragmentation metrics, per-km² zonal
  sums) must be reprojected to an equal-area CRS first — **EPSG:6933** (WGS 84
  / NSIDC EASE-Grid 2.0 Global, a Lambert cylindrical equal-area) is the
  default choice here. Do not compute areas or
  patch/edge metrics in degrees or in a non-equal-area projected CRS. A
  *ratio* computed entirely within EPSG:32648 (e.g. built-up pixel % per
  polygon) does not need this — UTM's conformal distortion cancels out of a
  within-polygon ratio as long as the AOI stays within one zone, clear of
  its edges. Reproject for this only once you need an absolute area.
- Spatial weights (`libpysal`) are scale- and topology-sensitive: note
  explicitly whether a given analysis uses Queen contiguity, KNN, or a
  distance band, and why — this changes what "neighbor" means and therefore
  what Moran's I is actually testing.
- Prefer vectorized shapely 2 / geopandas `sjoin`/`overlay` over Python loops.
- **OSM road class is not administrative class.** In Vietnam, provincial
  roads (`ref` "ĐT.279") are routinely tagged `highway=primary`, and national
  routes carry `ref` like "QL.18" (several joined by `;` or `,`). Derive
  "national highway" from `ref` (`desakota.highways.add_route_tier`), never
  from `highway=trunk/primary` alone. Expressways (`CT.*`) are
  access-controlled, so roadside ribbons are expected on the parallel
  national road, not on them. API and caveats: README "Highways" section.
- **OSM way length is carriageway-km, not route-km.** Divided roads are two
  one-way ways (49% of national-tier length is `oneway=yes`; QL5 sums to
  275 km, all of it one-way). Never sum `geometry.length` for route length or
  a ribbon length density; buffer and dissolve the centrelines first.
- **Coordinate order.** EPSG:4326's authority axis order is (lat, lon); OSM,
  Overpass and GeoJSON want (lon, lat). Use `pyproj.Transformer(...,
  always_xy=True)` when reprojecting bare geometries.

## Naming conventions

Full reference, with tables and the remaining deviations, is in README "Naming
conventions". The rules are below. Names carry what the type checker can't:
**unit** on every scalar and column, **grain** on the layer or file, and
**CRS** implied by having no suffix.

- **Layers** are the plural noun for one row (`communes`, `highways`,
  `ribbons`). Grids are named by cell size, with number and unit joined
  (`grid1km`). Single geometries get a singular role noun (`footprint`).
  Never `gdf`/`df`/`data`/`geom`.
- **CRS suffix:** no suffix = `CRS_UTM` (EPSG:32648). Any other copy takes
  `_wgs84` or `_ea`, including a source loaded in its native CRS
  (`communes_wgs84` → `communes`). Never EPSG digits (`aoi_32648`). The same
  rule applies to file stems in `processed/`.
- **Units suffix** is required: `_m`, `_km`, `_m2`, `_km2`, `_px`, `_s`,
  `_m_per_km2`, `n_` prefix for counts. Ratios are `_frac` (0–1, the default)
  vs `_pct` (0–100, only after an explicit `* 100`).
- **Coordinates** are `x`/`y` when projected and `lon`/`lat` when geographic.
  `bbox` is (minx, miny, maxx, maxy) and carries a CRS suffix.
- **Columns** are lowercase snake ASCII, IDs are `<entity>_id`, booleans use
  `is_`/`has_`. Source columns (GADM `NAME_3`) are renamed once, at load time.
  Values keep Vietnamese diacritics (NFC).
- **Rasters:** `lulc_path` (file) / `lulc_src` (dataset) / `lulc` (array) /
  `built_mask`. Pixel indices are `row`/`col`.
- **Weights and ESDA:** `w_queen`, `w_knn8`, `w_dist2km`, never a bare `w`.
  Results are named after the variable tested: `moran_<var>`, `lisa_<var>`.
- **Function verbs encode side effects:** `fetch_` (network, cached into
  `raw/`), `load_` (local read), `compute_` (pure → Series/array), `add_` (pure
  → copy with new columns), `build_` (pure → geometry/object), `detect_` (pure →
  new layer of features).
- **Files:** `processed/` stems are `<entity>_<measure>_<grain>[_<year>]`.
  `raw/` stems are `<source>_<cache key>` plus a `.json` sidecar. Notebooks are
  `NN_<topic>.ipynb`.

The codebase conforms as of 2026-10-10. New code follows these rules from the
start. If a deviation is ever unavoidable, list it under README "Remaining
deviations" rather than leaving it implicit.

## Adding dependencies

```bash
uv add <package>
uv add --dev <tool>
```

Add dependencies when an analysis needs them, not up front. Everything the
current phase needs is already declared in `pyproject.toml`.
