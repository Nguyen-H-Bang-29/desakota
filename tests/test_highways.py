"""Tests for the OSM highway fetcher. The Overpass call is always stubbed."""

import json
import time
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import geopandas as gpd
import osmnx as ox
import pandas as pd
import pytest
import requests
from shapely.geometry import LineString, Point, Polygon

from desakota.crs import CRS_UTM, CRS_WGS84
from desakota.highways import (
    DEFAULT_OVERPASS_URL,
    KEEP_COLUMNS,
    add_route_tier,
    build_query_polygon,
    compute_is_on_route,
    fetch_highways,
)

# 10 km x 10 km square near Bac Ninh, EPSG:32648 metres.
AOI_UTM = gpd.GeoDataFrame(
    geometry=[
        Polygon(
            [(600_000, 2_340_000), (610_000, 2_340_000), (610_000, 2_350_000), (600_000, 2_350_000)]
        )
    ],
    crs=CRS_UTM,
)


def _osm_response() -> gpd.GeoDataFrame:
    """A minimal stand-in for `ox.features_from_polygon`: mixed geometry types, sparse tags."""
    index = pd.MultiIndex.from_tuples(
        [('way', 1), ('way', 2), ('node', 3), ('way', 4)], names=['element', 'id']
    )
    return gpd.GeoDataFrame(
        {
            'highway': ['trunk', 'primary', 'primary', 'trunk'],
            'ref': ['QL.18', 'ĐT.279', None, 'QL.5'],
            'name': ['Quốc lộ 18', None, None, 'Quốc lộ 5'],
        },
        geometry=[
            LineString([(106.10, 21.10), (106.12, 21.12)]),
            LineString([(106.11, 21.11), (106.13, 21.10)]),
            Point(106.11, 21.11),
            Polygon([(106.1, 21.1), (106.11, 21.1), (106.11, 21.11), (106.1, 21.1)]),
        ],
        index=index,
        crs=CRS_WGS84,
    )


class _FakeOverpass:
    """Records calls to the stubbed `ox.features_from_polygon`.

    Each exception in `failures` is raised, in order, before calls start succeeding.
    """

    def __init__(
        self, failures: Sequence[Exception] = (), *, writes_http_cache: bool = False
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._failures = list(failures)
        self._writes_http_cache = writes_http_cache

    def __call__(self, polygon: Any, tags: dict[str, list[str]]) -> gpd.GeoDataFrame:
        # `overpass_url` is what osmnx would use at the moment of the request.
        self.calls.append(
            {'polygon': polygon, 'tags': tags, 'overpass_url': ox.settings.overpass_url}
        )
        if self._writes_http_cache:  # osmnx caches each response as it arrives
            folder = Path(ox.settings.cache_folder)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'response.json').write_text('{}')
        if self._failures:
            raise self._failures.pop(0)
        return _osm_response()


@pytest.fixture
def overpass(monkeypatch: pytest.MonkeyPatch) -> _FakeOverpass:
    fake = _FakeOverpass()
    monkeypatch.setattr(ox, 'features_from_polygon', fake)
    return fake


# --- build_query_polygon -----------------------------------------------------


def test_query_polygon_is_wgs84_lonlat_and_covers_the_aoi() -> None:
    # Act
    polygon = build_query_polygon(AOI_UTM, buffer_m=1_000, simplify_tol_m=100)

    # Assert: lon/lat order, and the original AOI (reprojected) sits inside it
    minx, miny, maxx, maxy = polygon.bounds
    assert 100 < minx < maxx < 110
    assert 15 < miny < maxy < 25
    aoi_wgs84 = AOI_UTM.to_crs(CRS_WGS84).union_all()
    assert polygon.contains(aoi_wgs84)


def test_query_polygon_rejects_empty_aoi() -> None:
    # Arrange
    aoi = gpd.GeoDataFrame(geometry=[], crs=CRS_UTM)

    # Act / Assert
    with pytest.raises(ValueError, match='polygonal'):
        build_query_polygon(aoi)


def test_query_polygon_rejects_geographic_aoi() -> None:
    # Arrange: buffering in metres is meaningless on a degree-based CRS
    aoi = AOI_UTM.to_crs(CRS_WGS84)

    # Act / Assert
    with pytest.raises(ValueError, match='projected'):
        build_query_polygon(aoi)


def test_query_polygon_rejects_missing_crs() -> None:
    # Arrange
    aoi = gpd.GeoDataFrame(geometry=list(AOI_UTM.geometry))

    # Act / Assert
    with pytest.raises(ValueError, match='CRS'):
        build_query_polygon(aoi)


def test_query_polygon_rejects_simplify_tolerance_not_smaller_than_buffer() -> None:
    # Act / Assert: otherwise simplification could pull the boundary inside the true AOI
    with pytest.raises(ValueError, match='tolerance'):
        build_query_polygon(AOI_UTM, buffer_m=100, simplify_tol_m=100)


# --- fetch_highways ----------------------------------------------------------


def test_fetch_keeps_only_lines_in_the_working_crs(overpass: _FakeOverpass, tmp_path: Path) -> None:
    # Act
    highways = fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert: the node and the closed-way polygon are dropped
    assert len(highways) == 2
    assert set(highways.geometry.geom_type) == {'LineString'}
    assert highways.crs == CRS_UTM
    assert sorted(highways['osm_id']) == [1, 2]


def test_fetch_collapses_ways_returned_by_several_subrequests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange: osmnx queries each sub-polygon separately, so a way that crosses a
    # sub-polygon boundary is returned once per request (seen on the full delta).
    base = _osm_response()
    duplicated = pd.concat([base, base.iloc[[0]]])

    def overpass(polygon: Any, tags: dict[str, list[str]]) -> gpd.GeoDataFrame:
        return duplicated

    monkeypatch.setattr(ox, 'features_from_polygon', overpass)

    # Act
    highways = fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert: without this, road length would be double-counted
    assert highways['osm_id'].is_unique
    assert sorted(highways['osm_id']) == [1, 2]


def test_fetch_returns_a_stable_schema_even_when_osm_omits_tags(
    overpass: _FakeOverpass, tmp_path: Path
) -> None:
    # Act
    highways = fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert: 'tunnel', 'lanes', ... never appeared in the response but are still columns
    assert list(highways.columns) == ['osm_id', *KEEP_COLUMNS, 'geometry']


def test_fetch_queries_overpass_in_wgs84_for_the_requested_classes(
    overpass: _FakeOverpass, tmp_path: Path
) -> None:
    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path, highway_classes=('trunk', 'primary'))

    # Assert
    (call,) = overpass.calls
    assert call['tags'] == {'highway': ['trunk', 'primary']}
    assert 100 < call['polygon'].bounds[0] < 110


def test_fetch_is_cached_on_disk(overpass: _FakeOverpass, tmp_path: Path) -> None:
    # Arrange
    first = fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Act
    second = fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert: one network call, identical content
    assert len(overpass.calls) == 1
    assert len(second) == len(first)
    assert second.crs == CRS_UTM
    assert list(second.columns) == list(first.columns)


def test_force_refetches(overpass: _FakeOverpass, tmp_path: Path) -> None:
    # Arrange
    fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path, force=True)

    # Assert
    assert len(overpass.calls) == 2


def test_cache_key_depends_on_classes_and_aoi(overpass: _FakeOverpass, tmp_path: Path) -> None:
    # Arrange
    moved = AOI_UTM.translate(xoff=50_000)

    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path, highway_classes=('trunk',))
    fetch_highways(AOI_UTM, cache_dir=tmp_path, highway_classes=('trunk', 'primary'))
    fetch_highways(gpd.GeoDataFrame(geometry=moved, crs=CRS_UTM), cache_dir=tmp_path)

    # Assert: three distinct queries, three network calls, three cache files
    assert len(overpass.calls) == 3
    assert len(list(tmp_path.glob('highways_*.gpkg'))) == 3


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replaces `time.sleep` and records the delays that were requested."""
    delays: list[float] = []
    monkeypatch.setattr(time, 'sleep', delays.append)
    return delays


@pytest.fixture
def no_sleep(slept: list[float]) -> None:
    del slept


def test_http_cache_is_cleared_after_a_successful_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    monkeypatch.setattr(ox, 'features_from_polygon', _FakeOverpass(writes_http_cache=True))

    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert: otherwise `force=True` would silently replay stale OSM data from osmnx's cache
    assert not (tmp_path / '_http').exists()


def test_force_gets_fresh_data_rather_than_replaying_the_http_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange: a leftover response from an earlier, failed run
    stale = tmp_path / '_http'
    stale.mkdir()
    (stale / 'old.json').write_text('{}')
    fake = _FakeOverpass()
    monkeypatch.setattr(ox, 'features_from_polygon', fake)

    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path, force=True)

    # Assert
    assert len(fake.calls) == 1
    assert not stale.exists()


def test_http_cache_is_kept_when_the_fetch_fails_so_a_rerun_can_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    dead = _FakeOverpass(
        failures=[requests.exceptions.ConnectionError('refused')], writes_http_cache=True
    )
    monkeypatch.setattr(ox, 'features_from_polygon', dead)

    # Act
    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_highways(AOI_UTM, cache_dir=tmp_path, max_attempts=1)

    # Assert
    assert (tmp_path / '_http' / 'response.json').exists()


def test_fetch_defaults_to_the_round_robin_alias_not_one_pinned_backend(
    overpass: _FakeOverpass, tmp_path: Path
) -> None:
    # Arrange: the alias has 2+ backends behind it; either one, pinned alone, has
    # been observed completely unreachable while the other (and the alias) was fine.
    # Pinning one removes the redundancy the alias exists to provide.

    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert
    (call,) = overpass.calls
    assert call['overpass_url'] == DEFAULT_OVERPASS_URL
    assert DEFAULT_OVERPASS_URL == 'https://overpass-api.de/api'


def test_fetch_uses_a_custom_overpass_url_and_restores_settings(
    overpass: _FakeOverpass, tmp_path: Path
) -> None:
    # Arrange
    before = (ox.settings.overpass_url, ox.settings.cache_folder, ox.settings.use_cache)

    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path, overpass_url='https://mirror.example/api')

    # Assert
    (call,) = overpass.calls
    assert call['overpass_url'] == 'https://mirror.example/api'
    assert (ox.settings.overpass_url, ox.settings.cache_folder, ox.settings.use_cache) == before


def test_settings_are_restored_even_when_the_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_sleep: None
) -> None:
    # Arrange
    before = (ox.settings.overpass_url, ox.settings.cache_folder, ox.settings.use_cache)
    monkeypatch.setattr(ox, 'features_from_polygon', _FakeOverpass(failures=[ValueError('boom')]))

    # Act
    with pytest.raises(ValueError, match='boom'):
        fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert
    assert (ox.settings.overpass_url, ox.settings.cache_folder, ox.settings.use_cache) == before


def test_retry_backoff_grows_exponentially_and_is_capped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slept: list[float]
) -> None:
    # Arrange: refusals can be server-side rate limiting, which a fixed short delay never outwaits
    dead = _FakeOverpass(failures=[requests.exceptions.ConnectionError('refused')] * 20)
    monkeypatch.setattr(ox, 'features_from_polygon', dead)

    # Act
    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_highways(AOI_UTM, cache_dir=tmp_path, max_attempts=8)

    # Assert
    assert slept == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0]


def test_fetch_retries_transient_connection_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_sleep: None
) -> None:
    # Arrange: overpass-api.de round-robins DNS over backends, one of which refuses
    # connections, so any single request can fail while an immediate retry succeeds.
    flaky = _FakeOverpass(failures=[requests.exceptions.ConnectionError('refused')] * 2)
    monkeypatch.setattr(ox, 'features_from_polygon', flaky)

    # Act
    highways = fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert
    assert len(flaky.calls) == 3
    assert len(highways) == 2


def test_fetch_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_sleep: None
) -> None:
    # Arrange
    dead = _FakeOverpass(failures=[requests.exceptions.ConnectionError('refused')] * 10)
    monkeypatch.setattr(ox, 'features_from_polygon', dead)

    # Act / Assert
    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_highways(AOI_UTM, cache_dir=tmp_path, max_attempts=3)
    assert len(dead.calls) == 3
    assert not list(tmp_path.glob('highways_*.gpkg'))


def test_fetch_does_not_retry_other_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_sleep: None
) -> None:
    # Arrange: a bad query is a bug, not a flaky network
    broken = _FakeOverpass(failures=[ValueError('bad query')])
    monkeypatch.setattr(ox, 'features_from_polygon', broken)

    # Act / Assert
    with pytest.raises(ValueError, match='bad query'):
        fetch_highways(AOI_UTM, cache_dir=tmp_path)
    assert len(broken.calls) == 1


def test_fetch_writes_provenance_metadata(overpass: _FakeOverpass, tmp_path: Path) -> None:
    # Act
    fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert: OSM changes daily, so raw data must record when it was pulled
    (meta_path,) = tmp_path.glob('highways_*.json')
    meta = json.loads(meta_path.read_text())
    assert meta['n_features'] == 2
    assert meta['highway_classes'] == ['motorway', 'trunk', 'primary']
    assert {'fetched_at', 'osmnx_version'} <= meta.keys()
    assert meta['overpass_url'] == DEFAULT_OVERPASS_URL


# --- add_route_tier ----------------------------------------------------------


def _tiered(refs: list[str | None], names: list[str | None] | None = None) -> gpd.GeoDataFrame:
    frame = gpd.GeoDataFrame(
        {'ref': refs, 'name': names if names is not None else [None] * len(refs)},
        geometry=[LineString([(0, 0), (1, 1)])] * len(refs),
        crs=CRS_UTM,
    )
    return add_route_tier(frame)


@pytest.mark.parametrize(
    ('ref', 'tier', 'route'),
    [
        ('QL.18', 'national', 'QL18'),
        ('QL5', 'national', 'QL5'),
        ('QL.1A', 'national', 'QL1A'),
        ('QL.18;QL.17', 'national', 'QL18'),
        ('QL.17;ĐT.287', 'national', 'QL17'),
        ('ĐT.279', 'provincial', None),
        ('ĐT.279, ĐT.287', 'provincial', None),
        ('DT.279', 'provincial', None),
        ('CT.04', 'expressway', None),
        # Real OSM tags: expressways are also numbered as the national route they
        # replace, and must still be classed as expressways.
        ('CT.01;QL.1', 'expressway', None),
        ('CT.09;CT.38;QL.18', 'expressway', None),
        ('TL.100', 'other', None),
        (None, 'other', None),
    ],
)
def test_route_tier(ref: str | None, tier: str, route: str | None) -> None:
    # Act
    result = _tiered([ref])

    # Assert
    assert result['tier'].iloc[0] == tier
    if route is None:
        assert pd.isna(result['route'].iloc[0])
    else:
        assert result['route'].iloc[0] == route


@pytest.mark.parametrize(
    ('name', 'tier', 'route'),
    [
        ('Quốc lộ 5', 'national', 'QL5'),
        ('Quốc lộ 1A', 'national', 'QL1A'),
        ('quốc lộ 18', 'national', 'QL18'),
        ('Đường tỉnh 285B', 'provincial', None),
        ('Tỉnh lộ 295', 'provincial', None),
        ('Đường cao tốc Nội Bài – Bắc Ninh', 'expressway', None),  # noqa: RUF001 -- verbatim OSM name (en dash)
        # Urban streets tagged primary/trunk are not administrative routes.
        ('Đường Lý Thái Tổ', 'other', None),
        ('Cầu Hà Bắc 2', 'other', None),
        (None, 'other', None),
    ],
)
def test_route_tier_falls_back_to_name_when_ref_is_missing(
    name: str | None, tier: str, route: str | None
) -> None:
    # Act
    result = _tiered([None], [name])

    # Assert
    assert result['tier'].iloc[0] == tier
    if route is None:
        assert pd.isna(result['route'].iloc[0])
    else:
        assert result['route'].iloc[0] == route


def test_route_tier_name_fallback_survives_unicode_decomposition() -> None:
    # Arrange: the same words with combining diacritics instead of precomposed letters
    decomposed = unicodedata.normalize('NFD', 'Quốc lộ 5')
    assert decomposed != 'Quốc lộ 5'

    # Act
    result = _tiered([None], [decomposed])

    # Assert
    assert result['tier'].iloc[0] == 'national'


def test_route_tier_prefers_ref_over_name() -> None:
    # Act: ref says provincial, name (a street label) mentions a national route
    result = _tiered(['ĐT.279'], ['Đoạn Quốc lộ 18 cũ'])

    # Assert
    assert result['tier'].iloc[0] == 'provincial'


def test_route_tier_does_not_mutate_its_input() -> None:
    # Arrange
    frame = gpd.GeoDataFrame(
        {'ref': ['QL.18'], 'name': [None]}, geometry=[LineString([(0, 0), (1, 1)])], crs=CRS_UTM
    )

    # Act
    add_route_tier(frame)

    # Assert
    assert 'tier' not in frame.columns


def test_route_tier_preserves_crs() -> None:
    assert _tiered(['QL.18']).crs == CRS_UTM


def test_retry_warning_names_the_underlying_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_sleep: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange: refused, DNS failure, reset and connect-timeout are all ConnectionError,
    # and each points at a different fix, so the log has to say which one it was.
    cause = requests.exceptions.ConnectionError('Name or service not known')
    monkeypatch.setattr(ox, 'features_from_polygon', _FakeOverpass(failures=[cause]))

    # Act
    with caplog.at_level('WARNING', logger='desakota.highways'):
        fetch_highways(AOI_UTM, cache_dir=tmp_path)

    # Assert
    assert 'Name or service not known' in caplog.text


# --- compute_is_on_route -----------------------------------------------------


def _ways(refs: list[str | None], names: list[str | None] | None = None) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {'ref': refs, 'name': names if names is not None else [None] * len(refs)},
        geometry=[LineString([(0, 0), (1, 1)])] * len(refs),
        crs=CRS_UTM,
    )


@pytest.mark.parametrize(
    ('ref', 'name', 'expected'),
    [
        ('QL.18', None, True),
        ('QL18', None, True),
        ('QL.18;QL.17', None, True),
        # `add_route_tier` keeps only the first national number; membership must not.
        ('QL.17;QL.18', None, True),
        ('ĐT.287, QL.18', None, True),
        # Expressway sections co-signed with the route are still *signed* QL18;
        # excluding them is the caller's tier decision, not this function's.
        ('CT.01;QL.1;QL.18', None, True),
        ('QL.18A', None, False),
        ('QL.180', None, False),
        ('QL.1', None, False),
        ('ĐT.279', None, False),
        (None, 'Quốc lộ 18', True),
        (None, 'Quốc lộ 18A', False),
        (None, 'Đường Trần Hưng Đạo', False),
        # A ref, when present, is authoritative over the name.
        ('ĐT.279', 'Đoạn Quốc lộ 18 cũ', False),
    ],
)
def test_is_on_route(ref: str | None, name: str | None, expected: bool) -> None:
    # Act
    result = compute_is_on_route(_ways([ref], [name]), 'QL18')

    # Assert
    assert bool(result.iloc[0]) is expected


def test_is_on_route_accepts_dotted_route_and_returns_aligned_bool_series() -> None:
    # Arrange
    ways = _ways(['QL.5', 'QL.18', None]).set_axis([10, 20, 30])

    # Act
    result = compute_is_on_route(ways, 'QL.18')

    # Assert
    assert result.dtype == bool
    assert list(result.index) == [10, 20, 30]
    assert list(result) == [False, True, False]


def test_is_on_route_rejects_a_non_national_route() -> None:
    with pytest.raises(ValueError, match='national'):
        compute_is_on_route(_ways(['ĐT.279']), 'ĐT.279')
