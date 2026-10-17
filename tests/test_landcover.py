"""Tests for the LULC clip and zonal stats. The remote COG is a local GeoTIFF."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pystac
import pytest
import rasterio
from shapely.geometry import box

from desakota.crs import CRS_UTM, CRS_WGS84
from desakota.landcover import compute_built_frac, fetch_landcover

LEGEND = {1: 'Water', 2: 'Trees', 7: 'Built area'}
ORIGIN_X, ORIGIN_Y = 600_000.0, 2_350_000.0  # EPSG:32648, top-left corner
SIZE = 100  # 100 x 100 pixels of 10 m = 1 km square


def _rio(fp: str | Path, mode: str = 'r', **profile: Any) -> Any:
    """`rasterio.open` typed as `Any`; its stubs are incomplete (see desakota.landcover)."""
    return rasterio.open(fp, mode, **profile)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- untyped C-extension stubs


@pytest.fixture
def source_tif(tmp_path: Path) -> Path:
    """Stand-in for an item COG: left half water, right half built, top-right row trees."""
    data = np.full((SIZE, SIZE), 1, dtype=np.uint8)
    data[:, 50:] = 7
    data[0, 50:] = 2
    path = tmp_path / 'source' / 'item.tif'
    path.parent.mkdir()
    profile: dict[str, Any] = {
        'driver': 'GTiff',
        'width': SIZE,
        'height': SIZE,
        'count': 1,
        'dtype': 'uint8',
        'crs': CRS_UTM,
        'transform': rasterio.Affine(10, 0, ORIGIN_X, 0, -10, ORIGIN_Y),  # 10 m, north-up
        'nodata': 0,
    }
    with _rio(path, 'w', **profile) as writer:
        writer.write(data, 1)
    return path


def _item(href: str) -> pystac.Item:
    item = pystac.Item(
        id='48Q-2023',
        geometry=None,
        bbox=None,
        datetime=None,  # like the real annual items: a range, no single datetime
        start_datetime=datetime(2023, 1, 1, tzinfo=UTC),
        end_datetime=datetime(2024, 1, 1, tzinfo=UTC),
        properties={},
        collection='io-lulc-annual-v02',
    )
    item.add_asset('data', pystac.Asset(href=href))
    return item


def _aoi(
    minx: float, miny: float, maxx: float, maxy: float, crs: str = CRS_UTM
) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[box(minx, miny, maxx, maxy)], crs=crs)


# --- fetch_landcover ---------------------------------------------------------


def test_clip_is_snapped_outward_to_the_pixel_grid(source_tif: Path, tmp_path: Path) -> None:
    # Arrange: bounds fall mid-pixel on every side
    aoi = _aoi(ORIGIN_X + 104, ORIGIN_Y - 296, ORIGIN_X + 596, ORIGIN_Y - 105)

    # Act
    path = fetch_landcover(aoi, _item(str(source_tif)), cache_dir=tmp_path / 'lulc')

    # Assert: whole pixels covering the AOI, values identical to the source window
    with _rio(path) as c, _rio(source_tif) as s:
        assert tuple(c.bounds) == (ORIGIN_X + 100, ORIGIN_Y - 300, ORIGIN_X + 600, ORIGIN_Y - 100)
        assert c.crs == s.crs
        np.testing.assert_array_equal(c.read(1), s.read(1)[10:30, 10:60])


def test_sidecar_records_provenance_without_the_sas_token(
    source_tif: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a signed href, as planetary_computer.sign_inplace would produce
    signed = 'https://example.blob.core.windows.net/lulc/48Q-2023.tif?sv=2021&sig=SECRET'
    item = _item(signed)
    real_open: Any = rasterio.open  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- untyped C-extension stubs

    def redirect(fp: str | Path, mode: str = 'r', **profile: Any) -> Any:
        return real_open(source_tif if fp == signed else fp, mode, **profile)

    monkeypatch.setattr(rasterio, 'open', redirect)

    # Act
    path = fetch_landcover(
        _aoi(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 500, ORIGIN_Y), item, cache_dir=tmp_path
    )

    # Assert
    sidecar = json.loads(path.with_suffix('.json').read_text())
    assert sidecar['href'] == 'https://example.blob.core.windows.net/lulc/48Q-2023.tif'
    assert 'SECRET' not in path.with_suffix('.json').read_text()
    assert sidecar['item_id'] == '48Q-2023'
    assert sidecar['collection'] == 'io-lulc-annual-v02'
    assert sidecar['item_start_datetime'] == '2023-01-01T00:00:00+00:00'
    assert sidecar['item_end_datetime'] == '2024-01-01T00:00:00+00:00'
    assert sidecar['window'] == {'col_off': 0, 'row_off': 0, 'width': 50, 'height': 50}


def test_cached_clip_is_reused_unless_forced(source_tif: Path, tmp_path: Path) -> None:
    # Arrange
    aoi = _aoi(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 500, ORIGIN_Y)
    item = _item(str(source_tif))
    first = fetch_landcover(aoi, item, cache_dir=tmp_path)
    stamp = first.stat().st_mtime_ns
    sidecar_before = first.with_suffix('.json').read_text()

    # Act / Assert: same query -> untouched file
    assert fetch_landcover(aoi, item, cache_dir=tmp_path) == first
    assert first.stat().st_mtime_ns == stamp

    # Act / Assert: force -> rewritten
    fetch_landcover(aoi, item, cache_dir=tmp_path, force=True)
    assert first.with_suffix('.json').read_text() != sidecar_before
    assert not list(tmp_path.glob('*.partial.tif'))


def test_sub_pixel_jitter_hits_the_same_cache_entry(source_tif: Path, tmp_path: Path) -> None:
    # Arrange
    item = _item(str(source_tif))

    # Act
    a = fetch_landcover(
        _aoi(ORIGIN_X + 101, ORIGIN_Y - 299, ORIGIN_X + 599, ORIGIN_Y - 101),
        item,
        cache_dir=tmp_path,
    )
    b = fetch_landcover(
        _aoi(ORIGIN_X + 102, ORIGIN_Y - 298, ORIGIN_X + 598, ORIGIN_Y - 102),
        item,
        cache_dir=tmp_path,
    )

    # Assert
    assert a == b


def test_buffer_widens_the_window(source_tif: Path, tmp_path: Path) -> None:
    # Act
    path = fetch_landcover(
        _aoi(ORIGIN_X + 200, ORIGIN_Y - 400, ORIGIN_X + 400, ORIGIN_Y - 200),
        _item(str(source_tif)),
        cache_dir=tmp_path,
        buffer_m=50,
    )

    # Assert
    with _rio(path) as c:
        assert (c.width, c.height) == (30, 30)


def test_aoi_outside_the_item_footprint_is_rejected(source_tif: Path, tmp_path: Path) -> None:
    # Arrange: crosses the east edge, i.e. would need the neighbouring item
    aoi = _aoi(ORIGIN_X + 800, ORIGIN_Y - 500, ORIGIN_X + 1_200, ORIGIN_Y)

    # Act / Assert
    with pytest.raises(ValueError, match='mosaic'):
        fetch_landcover(aoi, _item(str(source_tif)), cache_dir=tmp_path)
    assert not list(tmp_path.glob('*.tif'))


def test_crs_mismatch_is_rejected_not_reprojected(source_tif: Path, tmp_path: Path) -> None:
    # Arrange
    aoi = _aoi(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 500, ORIGIN_Y).to_crs(CRS_WGS84)

    # Act / Assert
    with pytest.raises(ValueError, match='does not match'):
        fetch_landcover(aoi, _item(str(source_tif)), cache_dir=tmp_path)


def test_missing_crs_is_rejected(source_tif: Path, tmp_path: Path) -> None:
    # Arrange
    aoi = gpd.GeoDataFrame(geometry=[box(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 500, ORIGIN_Y)])

    # Act / Assert
    with pytest.raises(ValueError, match='no CRS'):
        fetch_landcover(aoi, _item(str(source_tif)), cache_dir=tmp_path)


# --- compute_built_frac ------------------------------------------------------


def _built_frac(aoi: gpd.GeoDataFrame, raster: Path) -> pd.Series:
    return compute_built_frac(
        aoi, raster, LEGEND, built_area_label='Built area', water_label='Water'
    )


def test_built_frac_excludes_water_from_the_denominator(source_tif: Path) -> None:
    # Arrange: rows 1-10 (no tree row), straddling water | built; 50 built of 50 non-water
    aoi = _aoi(ORIGIN_X, ORIGIN_Y - 110, ORIGIN_X + 1_000, ORIGIN_Y - 10)

    # Act / Assert
    assert _built_frac(aoi, source_tif).tolist() == [1.0]


def test_built_frac_is_zero_not_nan_where_class_is_absent(source_tif: Path) -> None:
    # Arrange: two polygons, neither containing any built pixel; one also mixes trees in
    aoi = gpd.GeoDataFrame(
        geometry=[
            box(ORIGIN_X + 500, ORIGIN_Y - 10, ORIGIN_X + 1_000, ORIGIN_Y),  # trees only
            box(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 500, ORIGIN_Y),  # water only
        ],
        crs=CRS_UTM,
    )

    # Act
    built_frac = _built_frac(aoi, source_tif)

    # Assert: no built pixels -> 0.0; no non-water pixels -> NaN (undefined)
    assert built_frac.iloc[0] == 0.0
    assert np.isnan(built_frac.iloc[1])


def test_built_frac_keeps_the_aoi_index(source_tif: Path) -> None:
    # Arrange: non-range index, as after filtering communes
    aoi = _aoi(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 1_000, ORIGIN_Y).set_axis([42])

    # Act / Assert
    assert _built_frac(aoi, source_tif).index.tolist() == [42]


def test_built_frac_rejects_aoi_beyond_raster(source_tif: Path) -> None:
    # Arrange
    aoi = _aoi(ORIGIN_X + 500, ORIGIN_Y - 500, ORIGIN_X + 1_500, ORIGIN_Y)

    # Act / Assert
    with pytest.raises(ValueError, match='beyond raster'):
        _built_frac(aoi, source_tif)


def test_built_frac_rejects_crs_mismatch(source_tif: Path) -> None:
    # Arrange
    aoi = _aoi(ORIGIN_X, ORIGIN_Y - 500, ORIGIN_X + 500, ORIGIN_Y).to_crs(CRS_WGS84)

    # Act / Assert
    with pytest.raises(ValueError, match='does not match'):
        _built_frac(aoi, source_tif)
