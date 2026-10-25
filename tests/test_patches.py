"""Tests for patch detection and the alpha-shape elongation measure."""

import math

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, MultiLineString, Polygon, box

from desakota.crs import CRS_UTM
from desakota.patches import add_road_measures, compute_elongation, detect_patches

EDGE_M = 531.4  # H3 res 8 hexagon edge
SPACING_M = EDGE_M * math.sqrt(3)  # centre-to-centre distance of neighbouring hexagons


def _hexagon(x: float, y: float) -> Polygon:
    angles = np.deg2rad(np.arange(0, 360, 60) + 30)  # pointy-top
    return Polygon(np.c_[x + EDGE_M * np.cos(angles), y + EDGE_M * np.sin(angles)])


def _cells(hexagons: list[Polygon], is_member: list[bool] | None = None) -> gpd.GeoDataFrame:
    flags = is_member if is_member is not None else [True] * len(hexagons)
    return gpd.GeoDataFrame({'is_member': flags}, geometry=hexagons, crs=CRS_UTM)


def _strip(n: int, y: float = 0.0) -> list[Polygon]:
    return [_hexagon(i * SPACING_M, y) for i in range(n)]


def _blob(n_side: int) -> list[Polygon]:
    return [
        _hexagon(i * SPACING_M + (j % 2) * SPACING_M / 2, j * 1.5 * EDGE_M)
        for i in range(n_side)
        for j in range(n_side)
    ]


def _arc(n: int, radius_m: float = 8_000.0) -> list[Polygon]:
    """Touching hexagons along a quarter circle: a ribbon following a bending road."""
    step = SPACING_M / radius_m
    return [
        _hexagon(radius_m * math.cos(i * step), radius_m * math.sin(i * step)) for i in range(n)
    ]


def _detect(cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return detect_patches(cells, cells['is_member'], alpha_radius_m=1.5 * EDGE_M)


# --- compute_elongation ------------------------------------------------------


def test_elongation_of_a_rectangle_is_its_aspect_ratio() -> None:
    outlines = gpd.GeoSeries([box(0, 0, 4_000, 1_000), box(0, 0, 1_000, 1_000)], crs=CRS_UTM)

    assert compute_elongation(outlines).tolist() == pytest.approx([4.0, 1.0])


def test_elongation_of_a_shape_rounder_than_a_square_is_one() -> None:
    # A disc has P^2 < 16A, so no rectangle shares its area and perimeter.
    outlines = gpd.GeoSeries([box(0, 0, 1, 1).centroid.buffer(1_000)], crs=CRS_UTM)

    assert compute_elongation(outlines).tolist() == [1.0]


def test_elongation_keeps_the_index() -> None:
    outlines = gpd.GeoSeries([box(0, 0, 2, 1)], index=[7], crs=CRS_UTM)

    assert compute_elongation(outlines).index.tolist() == [7]


# --- detect_patches ----------------------------------------------------------


def test_adjacent_member_cells_form_one_patch_and_others_are_ignored() -> None:
    hexagons = [*_strip(4), *_strip(3, y=10_000)]  # two groups, far apart
    is_member = [True, True, True, True, True, True, False]

    patches = _detect(_cells(hexagons, is_member))

    assert sorted(patches['n_cells']) == [2, 4]
    assert patches.crs == CRS_UTM
    assert list(patches.columns) == [
        'patch_id',
        'n_cells',
        'area_km2',
        'elongation',
        'outline',
        'geometry',
    ]


def test_outline_is_an_alpha_shape_covering_the_patch_in_the_same_crs() -> None:
    patches = _detect(_cells(_strip(4)))

    outline = gpd.GeoSeries(patches['outline'])
    assert outline.crs == CRS_UTM
    # The outline fills notches between hexagons, so it covers the patch almost entirely.
    covered_m2 = outline.intersection(patches.geometry).area
    assert covered_m2.iloc[0] == pytest.approx(patches.area.iloc[0], rel=0.05)


def test_patch_area_is_the_area_of_its_cells() -> None:
    patches = _detect(_cells(_strip(4)))

    assert patches['area_km2'].iloc[0] == pytest.approx(4 * _hexagon(0, 0).area / 1e6)


def test_straight_strip_is_elongated_and_blob_is_not() -> None:
    strip = _detect(_cells(_strip(8)))['elongation'].iloc[0]
    blob = _detect(_cells(_blob(4)))['elongation'].iloc[0]

    assert 5.0 < strip < 9.0  # 8 cells long, 1 wide
    assert blob < 1.5


def test_curved_strip_scores_by_its_length_not_its_bounding_box() -> None:
    # A rotated rectangle around a quarter-circle ribbon is nearly square; the
    # alpha outline keeps the bend, so the ribbon still reads as long and thin.
    patches = _detect(_cells(_arc(12)))

    assert len(patches) == 1
    assert patches['elongation'].iloc[0] > 8.0


def test_no_members_gives_an_empty_layer_with_the_schema() -> None:
    patches = _detect(_cells(_strip(3), [False, False, False]))

    assert len(patches) == 0
    assert list(patches.columns) == [
        'patch_id',
        'n_cells',
        'area_km2',
        'elongation',
        'outline',
        'geometry',
    ]


def test_membership_must_align_with_cells() -> None:
    cells = _cells(_strip(3))

    with pytest.raises(ValueError, match='index'):
        detect_patches(cells, pd.Series([True, True]), alpha_radius_m=800.0)


# --- add_road_measures -------------------------------------------------------


def _strip_patches() -> gpd.GeoDataFrame:
    """One 5-hexagon strip along y = 0 and one blob 10 km away from it."""
    hexagons = [*_strip(5), *(shift_y(h, 10_000) for h in _blob(3))]
    return _detect(_cells(hexagons))


def shift_y(polygon: Polygon, dy: float) -> Polygon:
    return Polygon([(x, y + dy) for x, y in polygon.exterior.coords])


ROAD_ALONG_STRIP = LineString([(-5_000, 0), (20_000, 0)])
STRIP_LENGTH_M = 4 * SPACING_M + math.sqrt(3) * EDGE_M  # centre-to-centre span + one cell width


def test_road_cover_is_the_length_of_road_the_patch_lines() -> None:
    patches = add_road_measures(_strip_patches(), ROAD_ALONG_STRIP, near_m=500.0)

    strip, blob = patches.sort_values('n_cells').iloc[0], patches.sort_values('n_cells').iloc[1]
    assert strip['road_cover_m'] == pytest.approx(STRIP_LENGTH_M, rel=0.05)
    assert blob['road_cover_m'] == 0.0


def test_divided_road_is_not_counted_twice() -> None:
    divided = MultiLineString(
        [[(-5_000, -10), (20_000, -10)], [(-5_000, 10), (20_000, 10)]]  # 20 m apart
    )
    single = add_road_measures(_strip_patches(), ROAD_ALONG_STRIP, near_m=500.0)
    double = add_road_measures(_strip_patches(), divided, near_m=500.0)

    assert double['road_cover_m'].max() == pytest.approx(single['road_cover_m'].max(), rel=0.06)


def test_near_road_frac_is_the_share_of_patch_area_close_to_the_road() -> None:
    patches = add_road_measures(_strip_patches(), ROAD_ALONG_STRIP, near_m=500.0)
    by_size = patches.sort_values('n_cells')

    # Strip half-height is one edge (531 m), so almost all of it is within 500 m.
    assert by_size['near_road_frac'].iloc[0] > 0.9
    assert by_size['near_road_frac'].iloc[1] == 0.0


def test_road_measures_return_a_copy_with_the_new_columns() -> None:
    patches = _strip_patches()

    measured = add_road_measures(patches, ROAD_ALONG_STRIP, near_m=500.0)

    assert 'road_cover_m' not in patches.columns
    assert list(measured.columns[-2:]) == ['road_cover_m', 'near_road_frac']
    assert measured.index.equals(patches.index)
