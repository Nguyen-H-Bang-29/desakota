"""Tests for reading road centrelines from a Geofabrik "free" GeoPackage extract."""

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Polygon

from desakota.crs import CRS_UTM
from desakota.highways import GEOFABRIK_ROADS_LAYER, KEEP_COLUMNS, add_route_tier, load_highways

# 10 km x 10 km square near Bac Ninh, EPSG:32648 metres (about 106.0-106.1 E, 21.1-21.2 N).
AOI_UTM = gpd.GeoDataFrame(
    geometry=[
        Polygon(
            [(600_000, 2_340_000), (610_000, 2_340_000), (610_000, 2_350_000), (600_000, 2_350_000)]
        )
    ],
    crs=CRS_UTM,
)
_INSIDE = LineString([(106.02, 21.16), (106.04, 21.17)])
_FAR_AWAY = LineString([(105.20, 20.20), (105.21, 20.21)])


def _write_extract(path: Path) -> Path:
    """A Geofabrik-shaped roads layer: lon/lat (OGC:CRS84), T/F/B flags, '' for no tag."""
    roads = gpd.GeoDataFrame(
        {
            'osm_id': ['101', '102', '103', '104', '105'],
            'code': [5112, 5113, 5111, 5122, 5112],
            'fclass': ['trunk', 'primary', 'motorway', 'residential', 'trunk'],
            'name': ['Quốc lộ 18', '', 'Cầu Phù Lỗ', 'Ngõ 1', 'Quốc lộ 1'],
            'ref': ['QL.18', 'ĐT.279', 'CT.09;CT.38;QL.18', '', 'QL.1'],
            'oneway': ['F', 'B', 'T', 'B', 'F'],
            'maxspeed': [60, 0, 80, 0, 50],
            'layer': [0, 0, 1, 0, 0],
            'bridge': ['F', 'F', 'T', 'F', 'F'],
            'tunnel': ['F', 'F', 'F', 'F', 'F'],
        },
        geometry=[_INSIDE, _INSIDE, _INSIDE, _INSIDE, _FAR_AWAY],
        crs='OGC:CRS84',
    )
    roads.to_file(path, layer=GEOFABRIK_ROADS_LAYER, driver='GPKG')  # pyright: ignore[reportUnknownMemberType] -- geopandas leaves **kwargs untyped
    return path


@pytest.fixture
def extract(tmp_path: Path) -> Path:
    return _write_extract(tmp_path / 'vietnam.gpkg')


def test_load_returns_the_fetch_schema_in_the_working_crs(extract: Path) -> None:
    highways = load_highways(extract, AOI_UTM)

    assert highways.crs == CRS_UTM
    assert list(highways.columns) == ['osm_id', *KEEP_COLUMNS, 'geometry']
    assert highways['osm_id'].dtype == 'int64'
    for column in KEEP_COLUMNS:
        assert isinstance(highways[column].dtype, pd.StringDtype), column


def test_load_keeps_only_requested_classes_inside_the_aoi(extract: Path) -> None:
    highways = load_highways(extract, AOI_UTM)

    # 104 is residential, 105 lies ~100 km away.
    assert sorted(highways['osm_id']) == [101, 102, 103]


def test_load_honours_custom_classes(extract: Path) -> None:
    highways = load_highways(extract, AOI_UTM, highway_classes=['residential'])

    assert highways['osm_id'].tolist() == [104]


def test_load_translates_geofabrik_flags_to_osm_tag_values(extract: Path) -> None:
    by_id = load_highways(extract, AOI_UTM).set_index('osm_id')

    # F = one-way in digitised direction, T = reverse, B = both ways *or untagged*.
    assert by_id.loc[101, 'oneway'] == 'yes'
    assert by_id.loc[103, 'oneway'] == '-1'
    assert pd.isna(by_id.loc[102, 'oneway'])
    assert by_id.loc[103, 'bridge'] == 'yes'
    assert pd.isna(by_id.loc[101, 'bridge'])
    assert pd.isna(by_id.loc[101, 'tunnel'])
    assert by_id.loc[101, 'maxspeed'] == '60'
    assert pd.isna(by_id.loc[102, 'maxspeed'])  # 0 means "no maxspeed tag"
    assert by_id['lanes'].isna().all()  # not in the free export


def test_load_turns_empty_strings_into_missing_tags(extract: Path) -> None:
    by_id = load_highways(extract, AOI_UTM).set_index('osm_id')

    assert pd.isna(by_id.loc[102, 'name'])


def test_load_output_feeds_route_tiering(extract: Path) -> None:
    tiers = add_route_tier(load_highways(extract, AOI_UTM)).set_index('osm_id')['tier']

    assert tiers.to_dict() == {101: 'national', 102: 'provincial', 103: 'expressway'}


def test_load_rejects_classes_that_are_not_plain_identifiers(extract: Path) -> None:
    with pytest.raises(ValueError, match='highway class'):
        load_highways(extract, AOI_UTM, highway_classes=["trunk') OR 1=1 --"])


def test_load_reports_a_missing_extract(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match='Geofabrik'):
        load_highways(tmp_path / 'nope.gpkg', AOI_UTM)
