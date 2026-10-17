"""Fetch road centrelines from OpenStreetMap and tag them by administrative tier.

Why `features_from_polygon` and not `graph_from_polygon`: ribbon detection needs
each road as a single centreline geometry with its original tags. A routing graph
would split ways at every junction and duplicate two-way edges, which double-counts
length and discards `ref` semantics. This module never builds a graph.

Overpass only speaks WGS 84 lon/lat, so the AOI is reprojected to `CRS_WGS84` for
the query and the result comes back in the working CRS, `CRS_UTM` (metres).

Typical use::

    layout = DataLayout.from_env().ensure()
    highways = add_route_tier(fetch_highways(aoi, cache_dir=layout.raw / 'osm'))

`aoi` is any GeoDataFrame in a projected CRS. The first call for the whole delta
takes ~13 minutes (21 Overpass requests); later calls read the cached GeoPackage.
See the "Highways" section of README.md for the full guide and known caveats.
"""

import hashlib
import json
import logging
import re
import shutil
import time
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
import shapely
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import transform

from desakota.crs import CRS_UTM, CRS_WGS84

DEFAULT_HIGHWAY_CLASSES: Final[tuple[str, ...]] = ('motorway', 'trunk', 'primary')

KEEP_COLUMNS: Final[tuple[str, ...]] = (
    'highway',
    'ref',
    'name',
    'oneway',
    'lanes',
    'maxspeed',
    'bridge',
    'tunnel',
)
"""OSM tags kept. Overpass returns the union of tags seen, so the raw schema varies
by query; a fixed schema keeps downstream code and the on-disk cache stable."""

_LINE_TYPES: Final[tuple[str, ...]] = ('LineString', 'MultiLineString')

DEFAULT_OVERPASS_URL: Final[str] = 'https://overpass-api.de/api'
"""`overpass-api.de` is a round-robin alias for several backends (at least `z` and
`lz4`). A prior version of this constant pinned `z` after observing `lz4` refuse
every connection while `z` answered -- but the situation later reversed (`z`
unreachable, `lz4` and the alias both fine), which means pinning either single
backend is a worse bet than the alias: when your pinned choice is the one that's
down, every retry re-resolves to the same dead address and none can ever succeed,
whereas the alias at least has other backends for a retry to land on. Use the
alias and lean on `fetch_highways`'s retry/backoff for transient failures; pass
`overpass_url=` to pin a specific instance only if you have a concrete reason to
(e.g. you've just confirmed via /api/status which backend is healthy today)."""

_RETRY_DELAY_S: Final[float] = 3.0
_MAX_RETRY_DELAY_S: Final[float] = 60.0

logger = logging.getLogger(__name__)


def build_query_polygon(
    aoi: gpd.GeoDataFrame,
    *,
    buffer_m: float = 2_000.0,
    simplify_tol_m: float = 250.0,
) -> Polygon | MultiPolygon:
    """Build the WGS 84 polygon that is sent to Overpass.

    The AOI is dissolved, buffered outward, then simplified. Overpass rejects
    queries with very large polygon strings, and a commune-level union has
    hundreds of thousands of vertices. Buffering *before* simplifying, with a
    tolerance smaller than the buffer, guarantees the simplified boundary stays
    outside the true AOI, so no road inside it is lost. The buffer also keeps
    roads that leave and re-enter the AOI intact.

    Args:
        aoi: Study area in a projected CRS (metres).
        buffer_m: Outward buffer in metres.
        simplify_tol_m: Douglas-Peucker tolerance in metres; must be < `buffer_m`.

    Returns:
        Polygon in EPSG:4326, coordinate order (lon, lat).

    Raises:
        ValueError: If the AOI has no CRS, is not projected, is empty, or the
            tolerance is not smaller than the buffer.
    """
    if aoi.crs is None:
        raise ValueError('AOI has no CRS; refusing to guess one.')
    if not aoi.crs.is_projected:
        raise ValueError(f'AOI must be in a projected CRS (metres) to buffer; got {aoi.crs}.')
    if simplify_tol_m >= buffer_m:
        raise ValueError(
            f'simplify tolerance ({simplify_tol_m} m) must be smaller than the buffer '
            f'({buffer_m} m), or simplification can pull the boundary inside the AOI.'
        )

    footprint = aoi.geometry.union_all().buffer(buffer_m).simplify(simplify_tol_m)
    if footprint.is_empty or not isinstance(footprint, Polygon | MultiPolygon):
        raise ValueError('AOI has no polygonal extent to query.')
    # always_xy: EPSG:4326's authority axis order is (lat, lon); Overpass wants (lon, lat).
    to_wgs84 = Transformer.from_crs(aoi.crs, CRS_WGS84, always_xy=True)
    return transform(to_wgs84.transform, footprint)


def fetch_highways(
    aoi: gpd.GeoDataFrame,
    *,
    cache_dir: Path,
    highway_classes: Sequence[str] = DEFAULT_HIGHWAY_CLASSES,
    buffer_m: float = 2_000.0,
    force: bool = False,
    max_attempts: int = 12,
    overpass_url: str = DEFAULT_OVERPASS_URL,
) -> gpd.GeoDataFrame:
    """Return OSM road centrelines covering `aoi`, cached on disk.

    OSM changes daily, so a fetch is not reproducible from code alone. Each result
    is written once to `cache_dir` together with a JSON sidecar recording when and
    how it was pulled, and is only refetched when `force=True` or the query (AOI
    shape, buffer, classes) changes.

    osmnx splits a large AOI into many Overpass requests (21 for the whole delta),
    so one refused connection would otherwise discard the whole fetch. Connection
    errors are retried with exponential backoff (3 s, 6 s, ... capped at 60 s, since
    a refusal can be server-side rate limiting that a short delay never outwaits).
    osmnx keeps each completed response in `cache_dir/_http`, so a retry, or a
    rerun after a failure, resumes where it stopped. That folder is scratch space:
    it is deleted once the result is written, so a later fetch never replays stale
    OSM data. If a failed fetch is being resumed much later, delete `_http` first.

    Args:
        aoi: Study area in a projected CRS (metres).
        cache_dir: Directory for the cached GeoPackage and sidecar; created if
            missing. Intended to be `DataLayout.raw / 'osm'`.
        highway_classes: OSM `highway=*` values to request.
        buffer_m: Outward buffer around the AOI, in metres.
        force: Discard any cached result and pull fresh data from Overpass,
            including any partial responses left in `_http`.
        max_attempts: Tries per fetch before a connection error is re-raised.
        overpass_url: Overpass API base URL, without `/interpreter`. See
            `DEFAULT_OVERPASS_URL` for why this is the round-robin alias rather
            than a pinned backend.

    Returns:
        Line geometries in EPSG:32648 with columns `osm_id`, `KEEP_COLUMNS`
        (all string-typed, missing tags are NA) and `geometry`.

    Raises:
        requests.exceptions.ConnectionError: If every attempt failed to connect.
    """
    polygon = build_query_polygon(aoi, buffer_m=buffer_m)
    stem = f'highways_{_cache_key(polygon, highway_classes)}'
    data_path = cache_dir / f'{stem}.gpkg'

    if force or not data_path.exists():
        http_cache = cache_dir / '_http'
        cache_dir.mkdir(parents=True, exist_ok=True)
        if force:
            shutil.rmtree(http_cache, ignore_errors=True)
        with _osmnx_settings(cache_folder=http_cache, overpass_url=overpass_url):
            raw = _download_with_retry(polygon, list(highway_classes), max_attempts)
        tidy = _tidy(raw)

        partial = cache_dir / f'{stem}.partial.gpkg'
        tidy.to_file(partial, driver='GPKG')  # pyright: ignore[reportUnknownMemberType] -- geopandas leaves **kwargs untyped
        partial.replace(data_path)
        (cache_dir / f'{stem}.json').write_text(
            json.dumps(
                {
                    'fetched_at': datetime.now(UTC).isoformat(),
                    'osmnx_version': ox.__version__,
                    'overpass_url': overpass_url,
                    'highway_classes': list(highway_classes),
                    'buffer_m': buffer_m,
                    'query_bounds_wgs84': list(polygon.bounds),
                    'n_features': len(tidy),
                },
                indent=2,
            )
        )
        shutil.rmtree(http_cache, ignore_errors=True)  # only needed to resume a failed fetch

    # Fresh and cached calls share one return path so their dtypes can't diverge.
    return _read(data_path)


GEOFABRIK_ROADS_LAYER: Final[str] = 'gis_osm_roads_free'
"""Road layer in Geofabrik's "free" GIS export (`<region>-<yymmdd>-free.gpkg.zip`).
Layer documentation: https://download.geofabrik.de/osm-data-in-gis-formats-free.pdf"""

_CLASS_NAME: Final = re.compile(r'^[a-z_]+$')

# The free export flattens tags: oneway is F (one-way, digitised direction),
# T (one-way, reverse) or B (two-way *or untagged*, indistinguishable), bridge and
# tunnel are T/F, and 0 means "no maxspeed". Map back to OSM tag values, with NA
# wherever the export cannot tell "no" from "not tagged".
_ONEWAY_TAG: Final[dict[str, str]] = {'F': 'yes', 'T': '-1'}
_FLAG_TAG: Final[dict[str, str]] = {'T': 'yes'}


def load_highways(
    gpkg_path: Path,
    aoi: gpd.GeoDataFrame,
    *,
    highway_classes: Sequence[str] = DEFAULT_HIGHWAY_CLASSES,
    buffer_m: float = 2_000.0,
) -> gpd.GeoDataFrame:
    """Read road centrelines covering `aoi` from a local Geofabrik extract.

    The offline replacement for `fetch_highways`: the public Overpass instance no
    longer serves scripted clients, while a dated Geofabrik extract is one download
    with a fixed OSM timestamp (see the README inside the zip). Ways are whole OSM
    ways, as with Overpass, and the result has the same schema, so `add_route_tier`
    and `compute_is_on_route` apply unchanged. Two differences: `lanes` is always NA
    (not exported), and `oneway` is NA for both two-way and untagged roads.

    The AOI is buffered and simplified exactly as for an Overpass query
    (`build_query_polygon`), then used as a spatial mask on the read, so only
    features intersecting it are decoded. The extract is lon/lat (OGC:CRS84).

    Args:
        gpkg_path: The unzipped `.gpkg` from `<region>-<yymmdd>-free.gpkg.zip`,
            stored under `DataLayout.raw / 'osm'`.
        aoi: Study area in a projected CRS (metres).
        highway_classes: OSM `highway=*` values (Geofabrik `fclass`) to keep.
        buffer_m: Outward buffer around the AOI, in metres.

    Returns:
        Line geometries in EPSG:32648 with columns `osm_id` (int64), `KEEP_COLUMNS`
        (all string-typed, missing tags are NA) and `geometry`.

    Raises:
        FileNotFoundError: If `gpkg_path` does not exist.
        ValueError: If a class is not a plain lowercase identifier (it is
            interpolated into an OGR SQL filter), or the AOI is invalid.
    """
    if not gpkg_path.is_file():
        raise FileNotFoundError(f'Geofabrik extract not found: {gpkg_path}')
    for name in highway_classes:
        if not _CLASS_NAME.match(name):
            raise ValueError(f'Not a valid highway class: {name!r}')

    polygon = build_query_polygon(aoi, buffer_m=buffer_m)
    classes_sql = ', '.join(f"'{name}'" for name in highway_classes)
    roads = gpd.read_file(  # pyright: ignore[reportUnknownMemberType] -- geopandas leaves **kwargs untyped
        gpkg_path,
        layer=GEOFABRIK_ROADS_LAYER,
        where=f'fclass IN ({classes_sql})',
        mask=polygon,  # same lon/lat order as the file
    )
    return _from_geofabrik(roads)


def _from_geofabrik(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    def tag(column: str) -> pd.Series[str]:
        values = roads[column].astype('string')
        return values.mask(values == '')

    maxspeed = roads['maxspeed'].astype('Int64')
    tags = {
        'highway': tag('fclass'),
        'ref': tag('ref'),
        'name': tag('name'),
        'oneway': roads['oneway'].map(_ONEWAY_TAG),
        'lanes': pd.Series(pd.NA, index=roads.index),
        'maxspeed': maxspeed.where(maxspeed > 0).astype('string'),
        'bridge': roads['bridge'].map(_FLAG_TAG),
        'tunnel': roads['tunnel'].map(_FLAG_TAG),
    }
    tidy = gpd.GeoDataFrame(
        {'osm_id': roads['osm_id'].astype('int64'), **tags},
        geometry=roads.geometry,
        crs=roads.crs,
    )
    return tidy.astype(dict.fromkeys(KEEP_COLUMNS, 'string')).to_crs(CRS_UTM).reset_index(drop=True)


def _download_with_retry(
    polygon: Polygon | MultiPolygon, classes: list[str], max_attempts: int
) -> gpd.GeoDataFrame:
    for attempt in range(1, max_attempts + 1):
        try:
            return ox.features_from_polygon(polygon, tags={'highway': classes})
        except requests.exceptions.ConnectionError as e:
            if attempt == max_attempts:
                raise
            delay = min(_RETRY_DELAY_S * 2 ** (attempt - 1), _MAX_RETRY_DELAY_S)
            # Refused, DNS failure, reset and connect-timeout are all ConnectionError;
            # log which one, or a failed run can't be diagnosed afterwards.
            logger.warning(
                'Overpass connection failed (attempt %d/%d): %s; retrying in %.0f s',
                attempt,
                max_attempts,
                e,
                delay,
            )
            time.sleep(delay)
    raise ValueError('max_attempts must be >= 1')


def _tidy(raw: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep line features only, flatten the (element, id) index, fix the schema.

    osmnx queries each sub-polygon of a large AOI separately, so a way crossing a
    sub-polygon boundary comes back once per request; keep one copy or road length
    is double-counted.
    """
    lines = raw.loc[raw.geometry.geom_type.isin(_LINE_TYPES)]
    flat = (
        lines.reset_index()
        .rename(columns={'id': 'osm_id'})
        .drop_duplicates(subset=['element', 'osm_id'])
    )
    columns = ['osm_id', *KEEP_COLUMNS, 'geometry']
    return (
        flat.reindex(columns=columns).astype(dict.fromkeys(KEEP_COLUMNS, 'string')).to_crs(CRS_UTM)
    )


def _read(path: Path) -> gpd.GeoDataFrame:
    stored = gpd.read_file(path)  # pyright: ignore[reportUnknownMemberType] -- geopandas leaves **kwargs untyped
    return stored.astype(dict.fromkeys(KEEP_COLUMNS, 'string')).to_crs(CRS_UTM)


def _cache_key(polygon: Polygon | MultiPolygon, classes: Sequence[str]) -> str:
    # Snap to ~1 m so float jitter between runs doesn't invalidate the cache.
    snapped = shapely.set_precision(polygon, 1e-5)  # pyright: ignore[reportUnknownMemberType] -- shapely stub leaves **kwargs untyped
    payload = f'{snapped.wkb_hex}|{",".join(sorted(classes))}'
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:12]


@contextmanager
def _osmnx_settings(*, cache_folder: Path, overpass_url: str) -> Generator[None]:
    """Configure osmnx for one fetch, then restore its global settings.

    osmnx splits large polygons into several Overpass requests; caching each one
    in `cache_folder` means a failure on request 6 of 21 doesn't redo the first
    five. Its default (`./cache`) would land in the repo root on /mnt/c.
    """
    previous = (ox.settings.use_cache, ox.settings.cache_folder, ox.settings.overpass_url)
    ox.settings.use_cache = True
    ox.settings.cache_folder = cache_folder
    ox.settings.overpass_url = overpass_url
    try:
        yield
    finally:
        ox.settings.use_cache, ox.settings.cache_folder, ox.settings.overpass_url = previous


# --- route tiers -------------------------------------------------------------
# OSM Vietnam writes route numbers into `ref`, e.g. "QL.18" (quoc lo, national),
# "ĐT.279" (duong tinh, provincial), and joins several with ";" or ",".
# `highway=trunk/primary` alone is NOT "national": provincial roads are often
# tagged primary, so the tier is derived from `ref`.
#
# Observed in live data (Bac Ninh, 2026): expressway ways carry the national
# route they replace too ("CT.01;QL.1"), so expressway must outrank national;
# and ~1/3 of ways have no `ref` at all. Most of those are city streets and
# bridges (correctly "other"), but some provincial roads and expressway sections
# are identifiable only by `name`, hence the fallback below.

_NATIONAL: Final = re.compile(r'^QL\.?(\d+[A-Z]?)$')
_PROVINCIAL: Final = re.compile(r'^(?:ĐT|DT)\.?\d+')
_EXPRESSWAY: Final = re.compile(r'^(?:CT|AH)\.?\d+')
_REF_SEPARATOR: Final = re.compile(r'[;,]')

_NAME_NATIONAL: Final = re.compile(r'quốc\s*lộ\s*(\d+[A-Z]?)', re.IGNORECASE)
_NAME_PROVINCIAL: Final = re.compile(r'(?:đường\s*tỉnh|tỉnh\s*lộ)\s*\d+', re.IGNORECASE)
_NAME_EXPRESSWAY: Final = re.compile(r'cao\s*tốc', re.IGNORECASE)


def add_route_tier(highways: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return a copy with `tier` and `route` columns.

    `tier` is the highest applicable of expressway > national > provincial >
    other, taken from `ref`; only when `ref` yields nothing is `name` consulted
    ("Quốc lộ 5", "Đường tỉnh 285B", "Đường cao tốc ..."). `route` is the first
    national route on the way, normalised to e.g. `QL18`, or None; a way signed
    "QL.17;QL.18" gets `QL17`, so to select every way of one route use
    `compute_is_on_route`, not `route ==`. Expressways
    are kept separate because they are access-controlled: buildings cannot front
    them, so roadside ribbon development is expected on the parallel national
    road instead.

    Args:
        highways: Output of `fetch_highways` (needs `ref` and `name` columns).

    Returns:
        A new GeoDataFrame; the input is not modified.
    """
    parsed = [
        _classify(ref, name) for ref, name in zip(highways['ref'], highways['name'], strict=True)
    ]
    tagged = highways.copy()
    tagged['tier'] = [tier for tier, _ in parsed]
    tagged['route'] = [route for _, route in parsed]
    return tagged


def compute_is_on_route(highways: gpd.GeoDataFrame, route: str) -> pd.Series[bool]:
    """Flag every way signed as national route `route`, wherever it sits in `ref`.

    Matching follows `add_route_tier`: `ref` is authoritative, and `name`
    ("Quốc lộ 18") is consulted only when `ref` has no recognisable code. Unlike
    the `route` column it checks *all* numbers on a way, so "QL.17;QL.18" counts
    for both. Expressway sections co-signed with the route ("CT.01;QL.1;QL.18")
    are included; combine with `tier == 'national'` to keep only the road that
    buildings can front.

    Args:
        highways: Needs `ref` and `name` columns, e.g. output of `fetch_highways`.
        route: National route number, "QL18" or "QL.18".

    Returns:
        Boolean Series aligned to `highways.index`.

    Raises:
        ValueError: If `route` is not a national route number.
    """
    match = _NATIONAL.match(re.sub(r'\s+', '', route).upper())
    if match is None:
        raise ValueError(f'{route!r} is not a national route number like "QL18".')
    number = match.group(1)
    flags = [
        number in _national_numbers(ref, name)
        for ref, name in zip(highways['ref'], highways['name'], strict=True)
    ]
    return pd.Series(flags, index=highways.index, dtype=bool)


def _national_numbers(ref: object, name: object) -> set[str]:
    if _parse_ref(ref)[0] != 'other':
        assert isinstance(ref, str)  # _parse_ref returns 'other' for non-strings
        tokens = [re.sub(r'\s+', '', t).upper() for t in _REF_SEPARATOR.split(ref)]
        return {m.group(1) for t in tokens if (m := _NATIONAL.match(t))}
    if isinstance(name, str):
        text = unicodedata.normalize('NFC', name)
        return {m.group(1).upper() for m in _NAME_NATIONAL.finditer(text)}
    return set()


def _classify(ref: object, name: object) -> tuple[str, str | None]:
    by_ref = _parse_ref(ref)
    return by_ref if by_ref[0] != 'other' else _parse_name(name)


def _parse_name(name: object) -> tuple[str, str | None]:
    if not isinstance(name, str):
        return 'other', None

    # OSM text can be NFC or NFD; without this "Quốc" may silently fail to match.
    text = unicodedata.normalize('NFC', name)
    if _NAME_EXPRESSWAY.search(text):
        return 'expressway', None
    if match := _NAME_NATIONAL.search(text):
        return 'national', f'QL{match.group(1).upper()}'
    if _NAME_PROVINCIAL.search(text):
        return 'provincial', None
    return 'other', None


def _parse_ref(ref: object) -> tuple[str, str | None]:
    # `ref` may be None, pd.NA or NaN depending on how the frame was built.
    if not isinstance(ref, str):
        return 'other', None

    tokens = [re.sub(r'\s+', '', t).upper() for t in _REF_SEPARATOR.split(ref)]
    national = [m.group(1) for t in tokens if (m := _NATIONAL.match(t))]

    if any(_EXPRESSWAY.match(t) for t in tokens):
        return 'expressway', None
    if national:
        return 'national', f'QL{national[0]}'
    if any(_PROVINCIAL.match(t) for t in tokens):
        return 'provincial', None
    return 'other', None
