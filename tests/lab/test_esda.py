"""Tests for the Queen-contiguity weight builder."""

from typing import Any, cast

import geopandas as gpd
import pytest
from shapely.geometry import box

from desakota.crs import CRS_UTM
from desakota.lab.esda import build_queen_weights


def _units(*boxes: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[box(*b) for b in boxes], crs=CRS_UTM)


def test_weights_are_row_standardised_and_cover_every_unit() -> None:
    # Three squares in a row, each touching its neighbour on a shared edge.
    units = _units((0, 0, 1, 1), (1, 0, 2, 1), (2, 0, 3, 1))

    w = cast(Any, build_queen_weights(units))

    assert w.n == 3
    assert not list(w.islands)
    for i in range(w.n):
        assert sum(float(x) for x in w.weights[i]) == pytest.approx(1.0)


def test_islands_are_attached_to_their_nearest_unit() -> None:
    # Two adjacent squares plus a far-away sliver (an island under Queen contiguity).
    units = _units((0, 0, 1, 1), (1, 0, 2, 1), (100, 0, 101, 1))

    w = cast(Any, build_queen_weights(units))

    assert not list(w.islands)
    # The island (row 2) is attached to its nearest unit, which is row 1.
    assert 1 in list(w.neighbors[2])
    assert sum(float(x) for x in w.weights[2]) == pytest.approx(1.0)


def test_weights_follow_row_position_not_index() -> None:
    # A non-range index would otherwise flip "which unit is row 0" silently.
    units = _units((0, 0, 1, 1), (1, 0, 2, 1)).set_axis([42, 17])

    w = cast(Any, build_queen_weights(units))

    # Queen.from_dataframe is called with use_index=False, so keys are positions.
    assert sorted(int(k) for k in w.neighbors.keys()) == [0, 1]
