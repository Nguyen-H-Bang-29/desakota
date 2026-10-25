"""10m annual land-use/land-cover (LULC): clip from STAC, then zonal statistics.

Assumes a categorical raster (pixel value -> class id) and vector polygons
already in the raster's CRS — see `CLAUDE.md` on why that match is asserted
explicitly rather than left to fail silently.

Each STAC item in `io-lulc-annual-v02` is a whole UTM-zone x latitude-band
mosaic (e.g. `48Q`, ~6 deg x 8 deg), far larger than the delta, so
`fetch_landcover` reads only the AOI's window from the Cloud-Optimized GeoTIFF
over HTTP and caches that clip under `data/raw/`, with a sidecar recording
which item it came from.

rasterio's stubs don't cover its C-extension classes (`DatasetReader` etc.),
so every rasterio object is held as `Any` inside the functions here and only
plain `Path`/`pandas` types cross the module boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import geopandas as gpd
import numpy as np
import pandas as pd
import pystac
import pystac_client
import rasterio
import rasterstats
from rasterio.features import (
    rasterize,  # pyright: ignore[reportUnknownVariableType] -- rasterio stubs are incomplete
)
from rasterio.windows import Window
from rasterio.windows import (
    bounds as window_bounds,  # pyright: ignore[reportUnknownVariableType] -- rasterio stubs are incomplete
)
from shapely.geometry import box

ASSET_KEY: Final[str] = 'data'
"""Item asset holding the classified raster."""

_STRIP_ROWS: Final[int] = 2048
"""Rows read per request; bounds memory to ~50 MB per strip at uint8."""

_TESSELLATION_STRIP_ROWS: Final[int] = 512
"""Rows per strip when counting by cell; the int32 cell-id raster is 4 bytes/pixel."""


def _open(fp: str | Path, mode: str = 'r', **profile: Any) -> Any:
    """`rasterio.open`, typed as `Any`: the one place its unknown stub types are absorbed."""
    return rasterio.open(fp, mode, **profile)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- rasterio's C-extension stubs are incomplete (see module docstring)


def get_landcover_legend(
    collection: pystac_client.CollectionClient | pystac.Collection,
) -> dict[int, str]:
    """Extract the raster-value -> class-name lookup from STAC item metadata.

    Args:
        collection: A STAC collection whose `data` item asset carries the
            `file:values` extension (pixel value -> class label mapping).

    Returns:
        Mapping from raster pixel value to class label, e.g. `{7: "Built area"}`.
    """
    file_values = collection.item_assets[ASSET_KEY].properties['file:values']
    return {entry['values'][0]: entry['summary'] for entry in file_values}


def fetch_landcover(
    aoi: gpd.GeoDataFrame,
    item: pystac.Item,
    *,
    cache_dir: Path,
    buffer_m: float = 0.0,
    force: bool = False,
) -> Path:
    """Clip one LULC item to `aoi` with a windowed read, cached on disk.

    Only the pixels under the AOI's bounding box (plus `buffer_m`) are read from
    the remote COG; the window is snapped outward to the item's pixel grid, so
    the clip is pixel-for-pixel identical to the source (no resampling) and the
    cache key is stable across float jitter in the AOI. The result is written
    once with a JSON sidecar and only re-read when `force=True` or the item or
    window changes.

    Args:
        aoi: Study area, in the item's CRS (EPSG:32648 for the delta). Not
            reprojected here: a mismatch raises.
        item: A signed STAC item (from `desakota.stac.create_client`, whose
            asset hrefs carry a short-lived SAS token).
        cache_dir: Directory for the clip and sidecar; created if missing.
            Intended to be `DataLayout.raw / 'lulc'`.
        buffer_m: Outward buffer around the AOI bounds, in metres.
        force: Re-read and overwrite even if a cached clip exists.

    Returns:
        Path to the clipped GeoTIFF (EPSG of the item, 10 m, uint8, nodata 0).

    Raises:
        ValueError: If `aoi` has no CRS, a CRS other than the item's, or extends
            beyond the item's footprint (mosaicking several items is not
            implemented, and a partial clip would silently undercount pixels).
    """
    href = item.assets[ASSET_KEY].href
    with _open(href) as reader:
        window = _aoi_window(aoi, reader, buffer_m)
        key = _cache_key(item.id, window)
        stem = f'lulc_{item.id}_{key}'
        data_path = cache_dir / f'{stem}.tif'
        if force or not data_path.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            partial = cache_dir / f'{stem}.partial.tif'
            _copy_window(reader, window, partial)
            partial.replace(data_path)
            _write_sidecar(cache_dir / f'{stem}.json', item, href, reader, window, buffer_m)
    return data_path


def _aoi_window(aoi: gpd.GeoDataFrame, src: Any, buffer_m: float) -> Window:
    """Pixel-snapped window covering the AOI bounds, validated against the raster."""
    if aoi.crs is None:
        raise ValueError('AOI has no CRS; set it explicitly before clipping')
    if aoi.crs != src.crs:
        raise ValueError(f'AOI CRS {aoi.crs} does not match raster CRS {src.crs}')
    minx, miny, maxx, maxy = aoi.total_bounds
    inverse: Any = ~src.transform
    col_min, row_min = inverse @ (minx - buffer_m, maxy + buffer_m)
    col_max, row_max = inverse @ (maxx + buffer_m, miny - buffer_m)
    col_off, row_off = math.floor(col_min), math.floor(row_min)
    width, height = math.ceil(col_max) - col_off, math.ceil(row_max) - row_off
    if col_off < 0 or row_off < 0 or col_off + width > src.width or row_off + height > src.height:
        raise ValueError(
            f'AOI (+{buffer_m} m) extends beyond the raster footprint {tuple(src.bounds)}; '
            'it spans several items, which this function does not mosaic'
        )
    return Window(col_off, row_off, width, height)  # pyright: ignore[reportCallIssue] -- Window is an attrs class the stubs leave untyped


def _copy_window(src: Any, window: Window, dst_path: Path) -> None:
    """Stream `window` of band 1 into a tiled, LZW-compressed GeoTIFF, strip by strip."""
    win: Any = window
    profile: dict[str, Any] = {
        **src.profile,
        'driver': 'GTiff',
        'width': win.width,
        'height': win.height,
        'transform': src.window_transform(window),
        'compress': 'lzw',
        'tiled': True,
        'blockxsize': 512,
        'blockysize': 512,
        'BIGTIFF': 'IF_SAFER',
    }
    with _open(dst_path, 'w', **profile) as writer:
        for row in range(0, win.height, _STRIP_ROWS):
            n_rows = min(_STRIP_ROWS, win.height - row)
            strip = Window(win.col_off, win.row_off + row, win.width, n_rows)  # pyright: ignore[reportCallIssue] -- see _aoi_window
            writer.write(src.read(1, window=strip), 1, window=Window(0, row, win.width, n_rows))  # pyright: ignore[reportCallIssue] -- see _aoi_window
        try:  # io-lulc ships a palette; keep it so the clip renders correctly in QGIS
            writer.write_colormap(1, src.colormap(1))
        except ValueError:  # no colormap on this band
            pass


def _write_sidecar(
    path: Path, item: pystac.Item, href: str, src: Any, window: Window, buffer_m: float
) -> None:
    win: Any = window
    bounds: Any = src.window_bounds(window)
    path.write_text(
        json.dumps(
            {
                'fetched_at': datetime.now(UTC).isoformat(),
                'collection': item.collection_id,
                'item_id': item.id,
                # Annual items carry a start/end range, not a single `datetime`.
                'item_datetime': _iso(item.datetime),
                'item_start_datetime': _iso(item.common_metadata.start_datetime),
                'item_end_datetime': _iso(item.common_metadata.end_datetime),
                'href': _strip_query(href),
                'rasterio_version': rasterio.__version__,
                'gdal_version': rasterio.__gdal_version__,  # pyright: ignore[reportUnknownMemberType] -- untyped module attribute
                'crs': str(src.crs),
                'buffer_m': buffer_m,
                'window': {
                    'col_off': win.col_off,
                    'row_off': win.row_off,
                    'width': win.width,
                    'height': win.height,
                },
                'bounds': list(bounds),
            },
            indent=2,
        )
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _strip_query(href: str) -> str:
    """Drop the SAS token: it is a credential, and it expires within the hour anyway."""
    parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _cache_key(item_id: str, window: Window) -> str:
    win: Any = window
    payload = f'{item_id}|{win.col_off},{win.row_off},{win.width},{win.height}'
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:12]


def compute_built_frac(
    aoi: gpd.GeoDataFrame,
    raster_path: str | Path,
    legend: dict[int, str],
    built_area_label: str,
    water_label: str,
) -> pd.Series:
    """Compute each polygon's built-area share of its non-water pixels.

    Args:
        aoi: Polygons to compute zonal stats over. Must already share the
            raster's CRS and lie inside its extent — both raised explicitly
            rather than silently returning empty or partial stats
            (`rasterstats` neither reprojects nor warns).
        raster_path: Path to the categorical land-cover raster, e.g. the
            output of `fetch_landcover`.
        legend: Pixel value -> class-name mapping, from `get_landcover_legend`.
        built_area_label: Class name counted as the numerator, e.g. `"Built area"`.
        water_label: Class name excluded from the denominator, e.g. `"Water"`.

    Returns:
        Built-area fraction of non-water pixels per polygon, indexed like `aoi`.
        0.0 where a polygon has no built pixels; NaN only where it has no
        non-water pixels at all (the ratio is undefined there).

    Raises:
        ValueError: If `aoi.crs` does not match the raster's CRS, or `aoi`
            extends beyond the raster.
    """
    with _open(raster_path) as src:
        _require_inside(aoi, src)

    # rasterstats ships no stubs at all; categorical=True with the default
    # geojson_out=False returns one class-label -> pixel-count dict per polygon,
    # omitting classes with zero pixels (hence the fill below).
    stats: list[dict[str, int]] = rasterstats.zonal_stats(  # pyright: ignore
        vectors=aoi,
        raster=str(raster_path),
        categorical=True,
        nodata=0,
        category_map=legend,
    )
    counts = pd.DataFrame(stats, index=aoi.index)
    counts = counts.reindex(
        columns=counts.columns.union([built_area_label, water_label]), fill_value=0
    ).fillna(0)
    # pandas-stubs' DataFrame.drop/sum overloads are themselves only
    # partially typed (Unknown inside their own signatures) — not our bug.
    result: pd.Series = counts[built_area_label] / counts.drop(columns=water_label).sum(  # pyright: ignore
        axis=1
    )
    return result  # pyright: ignore


def compute_built_frac_for_tessellation(
    cells: gpd.GeoDataFrame,
    raster_path: str | Path,
    legend: dict[int, str],
    built_area_label: str,
    water_label: str,
) -> pd.Series:
    """`compute_built_frac` for many non-overlapping cells (a hex grid), in one pass.

    Each pixel is assigned to the cell containing its centre by rasterizing the cells
    onto the raster grid (GDAL's pixel-centre rule, as `rasterstats` uses by default),
    then built and non-water pixels are counted per cell with `np.bincount`. That is
    O(pixels) rather than one windowed read per polygon, so ~10^5 cells take seconds
    instead of hours. Because a pixel gets exactly one cell id, cells must not
    overlap; where they do, the later row silently wins.

    Args:
        cells: Non-overlapping polygons in the raster's CRS, inside its extent.
        raster_path: Categorical land-cover raster, e.g. from `fetch_landcover`.
        legend: Pixel value -> class name, from `get_landcover_legend`.
        built_area_label: Class counted as the numerator, e.g. `"Built area"`.
        water_label: Class excluded from the denominator, e.g. `"Water"`.

    Returns:
        Built-area fraction of non-water, non-nodata pixels per cell, indexed like
        `cells` and named `built_frac`. 0.0 with no built pixels; NaN where a cell
        holds no non-water pixel centre (open water, or a sliver smaller than a pixel).

    Raises:
        ValueError: If the CRS does not match the raster's or `cells` extend beyond it.
    """
    value_of = {label: value for value, label in legend.items()}
    built_value, water_value = value_of[built_area_label], value_of[water_label]
    n_cells = len(cells)
    built_px = np.zeros(n_cells + 1, dtype=np.int64)  # slot 0 = "no cell"
    valid_px = np.zeros(n_cells + 1, dtype=np.int64)
    geometries = cells.geometry.to_numpy()

    with _open(raster_path) as src:
        _require_inside(cells, src)
        nodata: int = int(src.nodata) if src.nodata is not None else 0
        col_off, row_off, width, height = _snapped_window(cells.total_bounds, src)
        for row in range(row_off, row_off + height, _TESSELLATION_STRIP_ROWS):
            n_rows = min(_TESSELLATION_STRIP_ROWS, row_off + height - row)
            strip = Window(col_off, row, width, n_rows)  # pyright: ignore[reportCallIssue] -- Window is an attrs class the stubs leave untyped
            hits = cells.sindex.query(box(*window_bounds(strip, src.transform)))
            if len(hits) == 0:
                continue
            cell_ids = np.asarray(
                rasterize(  # pyright: ignore[reportUnknownArgumentType] -- rasterio stubs are incomplete
                    ((geometries[i], int(i) + 1) for i in hits),
                    out_shape=(n_rows, width),
                    transform=src.window_transform(strip),
                    fill=0,
                    dtype='int32',
                ),
                dtype=np.int32,
            )
            lulc = np.asarray(src.read(1, window=strip), dtype=np.uint8)
            is_counted = (cell_ids > 0) & (lulc != nodata) & (lulc != water_value)
            valid_px += np.bincount(cell_ids[is_counted], minlength=n_cells + 1)
            built_px += np.bincount(
                cell_ids[is_counted & (lulc == built_value)], minlength=n_cells + 1
            )

    with np.errstate(invalid='ignore', divide='ignore'):
        frac = np.where(valid_px[1:] > 0, built_px[1:] / valid_px[1:], np.nan)
    return pd.Series(frac, index=cells.index, name='built_frac')


def _require_inside(aoi: gpd.GeoDataFrame, src: Any) -> None:
    """Raise unless `aoi` shares the raster's CRS and lies within its bounds."""
    if aoi.crs != src.crs:
        raise ValueError(f'AOI CRS {aoi.crs} does not match raster CRS {src.crs}')
    left, bottom, right, top = src.bounds
    minx, miny, maxx, maxy = aoi.total_bounds
    if minx < left or miny < bottom or maxx > right or maxy > top:
        raise ValueError(
            f'AOI bounds {aoi.total_bounds} extend beyond raster bounds '
            f'{(left, bottom, right, top)}; pixels outside would be silently uncounted'
        )


def _snapped_window(bbox: Any, src: Any) -> tuple[int, int, int, int]:
    """(col_off, row_off, width, height) covering `bbox` (minx, miny, maxx, maxy), clamped."""
    inverse: Any = ~src.transform
    col_min, row_min = inverse @ (bbox[0], bbox[3])
    col_max, row_max = inverse @ (bbox[2], bbox[1])
    col_off, row_off = max(math.floor(col_min), 0), max(math.floor(row_min), 0)
    width = min(math.ceil(col_max), src.width) - col_off
    height = min(math.ceil(row_max), src.height) - row_off
    return col_off, row_off, width, height
