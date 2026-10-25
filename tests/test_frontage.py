"""Tests for the chainage axis, road pieces and frontage pixel counts."""

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from shapely.geometry import LineString, MultiLineString

from desakota.crs import CRS_UTM
from desakota.frontage import build_reference_line, build_road_pieces
from desakota.landcover import compute_frontage_counts

LEGEND = {1: 'Water', 2: 'Trees', 7: 'Built area'}
ORIGIN_X, ORIGIN_Y = 600_000.0, 2_350_000.0  # EPSG:32648, top-left corner
SIZE = 100  # 100 x 100 pixels of 10 m = 1 km square
ROAD_Y = ORIGIN_Y - 500.0  # an east-west road through the middle


def _rio(fp: str | Path, mode: str = 'r', **profile: Any) -> Any:
    """`rasterio.open` typed as `Any`; its stubs are incomplete (see desakota.landcover)."""
    return rasterio.open(fp, mode, **profile)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- untyped C-extension stubs


@pytest.fixture
def ribbon_tif(tmp_path: Path) -> Path:
    """Trees everywhere, built in the 60 m north of the road (a one-sided ribbon)."""
    data = np.full((SIZE, SIZE), 2, dtype=np.uint8)
    data[44:50, 20:80] = 7  # 0-60 m north of ROAD_Y, along the road only (x 200-800 m)
    path = tmp_path / 'ribbon.tif'
    profile: dict[str, Any] = {
        'driver': 'GTiff',
        'width': SIZE,
        'height': SIZE,
        'count': 1,
        'dtype': 'uint8',
        'crs': CRS_UTM,
        'transform': rasterio.Affine(10, 0, ORIGIN_X, 0, -10, ORIGIN_Y),
        'nodata': 0,
    }
    with _rio(path, 'w', **profile) as writer:
        writer.write(data, 1)
    return path


def _east_west_road() -> LineString:
    return LineString([(ORIGIN_X + 200, ROAD_Y), (ORIGIN_X + 800, ROAD_Y)])


def _pieces(road: LineString | MultiLineString, station_m: float = 100.0) -> gpd.GeoDataFrame:
    reference = build_reference_line(road)
    return build_road_pieces(road, reference, piece_m=20.0, station_m=station_m).set_crs(CRS_UTM)


def test_reference_line_starts_west_even_if_mapped_east_to_west() -> None:
    road = LineString([(10_000, 0), (5_000, 100), (0, 0)])
    reference = build_reference_line(road)
    assert reference.coords[0] == (0.0, 0.0)
    assert reference.length == pytest.approx(road.length)


def test_reference_line_bridges_a_gap_with_one_straight_jump() -> None:
    road = MultiLineString([[(0, 0), (1_000, 0)], [(1_300, 0), (2_000, 0)]])
    reference = build_reference_line(road, max_gap_m=500)
    assert reference.length == pytest.approx(2_000)
    with pytest.raises(ValueError, match='cannot be joined'):
        build_reference_line(road, max_gap_m=200)


def test_reference_line_takes_one_carriageway_of_a_divided_road() -> None:
    # Single road, then two carriageways 20 m apart for 1 km, then single again.
    road = MultiLineString(
        [
            [(0, 0), (1_000, 0)],
            [(1_000, 0), (1_010, 10), (1_990, 10), (2_000, 0)],
            [(1_000, 0), (1_010, -10), (1_990, -10), (2_000, 0)],
            [(2_000, 0), (3_000, 0)],
        ]
    )
    reference = build_reference_line(road)
    assert reference.length == pytest.approx(3_000 + 2 * (np.hypot(10, 10) - 10), rel=1e-6)


def test_reference_line_penalty_trades_jumps_against_detours() -> None:
    # A U-shaped detour (900 m of road) whose ends are 400 m apart, then a 100 m gap.
    road = MultiLineString(
        [
            [(0, 0), (0, 300), (400, 300), (400, 100)],
            [(400, 0), (3_000, 0)],
        ]
    )
    # Penalty 5: 900 m of road + 5 x 100 m jump = 1,400 < 5 x 400 m shortcut = 2,000.
    assert build_reference_line(road, max_gap_m=500, gap_penalty=5).length == pytest.approx(
        900 + 100 + 2_600
    )
    # Penalty 1: the 400 m shortcut is cheaper than the detour, so it is taken.
    assert build_reference_line(road, max_gap_m=500, gap_penalty=1).length == pytest.approx(
        400 + 2_600
    )


def test_road_pieces_are_short_and_carry_chainage_and_station() -> None:
    pieces = _pieces(_east_west_road())
    assert (pieces.length <= 20.0 + 1e-9).all()
    assert pieces.length.sum() == pytest.approx(600)
    assert pieces['chainage_m'].is_monotonic_increasing
    assert pieces['station_id'].tolist() == (pieces['chainage_m'] // 100).astype(int).tolist()
    assert (pieces['direction_sign'] == 1).all()


def test_road_pieces_against_the_reference_get_negative_sign() -> None:
    # Divided road: the northern carriageway is mapped westbound (one-way).
    eastbound = LineString([(0, 0), (1_000, 0)])
    westbound = LineString([(1_000, 20), (0, 20)])
    road = MultiLineString([eastbound, westbound])
    reference = build_reference_line(road, max_gap_m=50)
    pieces = build_road_pieces(road, reference, piece_m=20.0, station_m=100.0)
    on_north = pieces.geometry.centroid.y > 10
    assert (pieces.loc[on_north, 'direction_sign'] == -1).all()
    assert (pieces.loc[~on_north, 'direction_sign'] == 1).all()


def test_road_pieces_reject_non_positive_lengths() -> None:
    road = _east_west_road()
    with pytest.raises(ValueError, match='positive'):
        build_road_pieces(road, road, piece_m=0, station_m=100)


def _frac(counts: pd.DataFrame, side: str, depth_max_m: float) -> float:
    """Built share of interior stations only; the road's rounded ends reach past the ribbon."""
    rows = counts[
        (counts['side'] == side)
        & (counts['depth_max_m'] == depth_max_m)
        & counts['station_id'].between(1, 4)
    ]
    return float(rows['n_built_px'].sum() / rows['n_valid_px'].sum())


def test_frontage_counts_separate_the_built_side_from_the_open_side(ribbon_tif: Path) -> None:
    counts = compute_frontage_counts(
        _pieces(_east_west_road()),
        ribbon_tif,
        LEGEND,
        'Built area',
        'Water',
        depth_edges_m=(0, 60, 200),
    )
    assert _frac(counts, 'left', 60) == pytest.approx(1.0)  # north = left of eastward
    assert _frac(counts, 'right', 60) == 0.0
    assert _frac(counts, 'left', 200) == 0.0
    assert sorted(counts['station_id'].unique().tolist()) == [0, 1, 2, 3, 4, 5]


def test_frontage_sides_agree_for_a_westbound_way(ribbon_tif: Path) -> None:
    road = _east_west_road().reverse()
    reference = _east_west_road()  # the axis still runs west to east
    pieces = build_road_pieces(road, reference, piece_m=20.0, station_m=100.0).set_crs(CRS_UTM)
    counts = compute_frontage_counts(
        pieces, ribbon_tif, LEGEND, 'Built area', 'Water', depth_edges_m=(0, 60)
    )
    assert _frac(counts, 'left', 60) == pytest.approx(1.0)
    assert _frac(counts, 'right', 60) == 0.0


def test_frontage_counts_leave_out_the_excluded_road_surface(ribbon_tif: Path) -> None:
    road = _east_west_road()
    pieces = _pieces(road)
    kwargs: dict[str, Any] = {'depth_edges_m': (0, 60)}
    full = compute_frontage_counts(pieces, ribbon_tif, LEGEND, 'Built area', 'Water', **kwargs)
    trimmed = compute_frontage_counts(
        pieces, ribbon_tif, LEGEND, 'Built area', 'Water', exclude=road.buffer(20), **kwargs
    )
    # 20 m of 60 m removed on each side, over a 600 m road (ends are rounded off).
    removed_px = full['n_valid_px'].sum() - trimmed['n_valid_px'].sum()
    assert removed_px == pytest.approx(2 * 600 * 20 / 100, rel=0.1)


def test_frontage_counts_count_each_pixel_once(ribbon_tif: Path) -> None:
    pieces = _pieces(_east_west_road(), station_m=50.0)
    counts = compute_frontage_counts(
        pieces, ribbon_tif, LEGEND, 'Built area', 'Water', depth_edges_m=(0, 100)
    )
    # Pixels within 100 m of a 600 m segment: 600 x 200 m plus two half-discs.
    expected_px = (600 * 200 + np.pi * 100**2) / 100
    assert counts['n_valid_px'].sum() == pytest.approx(expected_px, rel=0.02)


def test_frontage_counts_reject_bad_edges(ribbon_tif: Path) -> None:
    with pytest.raises(ValueError, match='increasing'):
        compute_frontage_counts(
            _pieces(_east_west_road()),
            ribbon_tif,
            LEGEND,
            'Built area',
            'Water',
            depth_edges_m=(0, 100, 50),
        )
