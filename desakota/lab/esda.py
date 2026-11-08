"""Spatial weights for exploratory spatial data analysis.

`libpysal` ships several contiguity and distance weight constructors, but the
corridor notebooks always want the same thing: Queen contiguity on the units
with islands rescued by their nearest neighbour, row-standardised so the spatial
lag is a weighted mean rather than a sum. Islands come up in practice on this
corridor — a hex sliver clipped off the coast has no edge neighbour — and if
they are left in, every ESDA statistic silently drops them.

Nothing here is reprojected; pass units already in the working CRS.
"""

from typing import Any, cast

import geopandas as gpd
import libpysal


def build_queen_weights(units: gpd.GeoDataFrame) -> libpysal.weights.W:
    """Queen contiguity on `units`, islands attached by KNN-1, row-standardised.

    A Queen neighbour shares at least one boundary point. Islands (units with no
    Queen neighbour, e.g. a sliver clipped off the coast) would otherwise be
    dropped by every ESDA statistic; each is attached to its single nearest unit
    via KNN-1. Weights are then row-standardised (`transform = 'r'`) so each
    row sums to 1, which turns the spatial lag into a weighted mean.

    Args:
        units: Polygon units in a projected CRS. `use_index=False` is forced on
            `libpysal`, so ordering follows the row position, not the index.

    Returns:
        A row-standardised `libpysal.weights.W`. `w.islands` is empty by
        construction; `w.n` equals `len(units)`.
    """
    w_queen = cast(
        Any,
        libpysal.weights.Queen.from_dataframe(  # pyright: ignore[reportUnknownMemberType] -- libpysal is untyped
            units, use_index=False, silence_warnings=True
        ),
    )
    if w_queen.islands:
        w_knn1 = cast(
            Any,
            libpysal.weights.KNN.from_dataframe(units, k=1, use_index=False),  # pyright: ignore[reportUnknownMemberType] -- libpysal is untyped
        )
        w_queen = libpysal.weights.attach_islands(w_queen, w_knn1)  # pyright: ignore[reportUnknownMemberType] -- libpysal is untyped
    w_queen.transform = 'r'
    return cast(libpysal.weights.W, w_queen)
