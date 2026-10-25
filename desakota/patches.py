"""Patches of adjacent grid cells, and how elongated they are.

A patch is a connected group of member cells (High-High in a LISA, or above a
built-up threshold). Its shape is measured on an **alpha shape** (Edelsbrunner,
Kirkpatrick & Seidel 1983) of the cells' vertices rather than on the raw union of
hexagons: the union's zigzag boundary inflates the perimeter, and the alpha shape
smooths it while still following bends, unlike a convex hull.

Shape alone cannot tell one ribbon along a road from a branching web of strips:
both have long, ragged outlines. `add_road_measures` therefore relates each patch
to a given road: how much of the road it lines, and how much of it lies near the
road.

Elongation is that of the **equivalent rectangle**, the rectangle with the same
area A and perimeter P: its sides are P/4 ± sqrt((P/4)² - A), and elongation is
long side / short side. A ribbon bending with the road keeps its length (≈ P/2)
and width (≈ 2A/P), whereas the minimum rotated rectangle of a curved strip is
nearly square. Shapes rounder than a square (P² < 16A) have no equivalent
rectangle and get elongation 1.
"""

import math
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from libpysal.cg import (
    alpha_shape,  # pyright: ignore[reportUnknownVariableType] -- libpysal is untyped
)
from numpy.typing import NDArray
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

PATCH_COLUMNS = ['patch_id', 'n_cells', 'area_km2', 'elongation', 'outline', 'geometry']

_ADJACENCY_TOLERANCE_M = 1.0
"""Cells are adjacent if within this distance; closes float gaps between clipped neighbours."""


def detect_patches(
    cells: gpd.GeoDataFrame, is_member: pd.Series, *, alpha_radius_m: float
) -> gpd.GeoDataFrame:
    """Group adjacent member cells into patches and measure each patch's elongation.

    Args:
        cells: Grid cells (e.g. from `build_hex_grid`) in a projected CRS (metres).
        is_member: Boolean flag per cell, aligned with `cells.index`.
        alpha_radius_m: Alpha-shape radius in metres: triangles of the cells'
            vertices with a larger circumradius are dropped. About 1.5 hexagon
            edges keeps a one-cell-wide strip in one piece and fills notches. It
            also fills every hole and inlet narrower than 2r (~2 cells), however
            deep: on the QL18 corridor's large patches the outline is 19-44%
            larger than the patch and reaches up to 3.4 km into long narrow gaps.

    Returns:
        One row per patch in the cells' CRS: `patch_id` (0..n-1, largest first),
        `n_cells`, `area_km2` (sum of its cells), `elongation` (equivalent
        rectangle of its alpha outline, >= 1), `outline` (that alpha shape, a
        second GeoSeries for maps) and `geometry` (union of its cells). A
        GeoPackage holds one geometry column, so drop `outline` before `to_file`.

    Raises:
        ValueError: If `is_member` is not indexed like `cells`.
    """
    if not is_member.index.equals(cells.index):
        raise ValueError('is_member must have the same index as cells.')
    members = cells.loc[is_member.astype(bool)]
    if members.empty:
        empty = gpd.GeoDataFrame(columns=PATCH_COLUMNS, geometry='geometry', crs=cells.crs)
        return empty.assign(outline=gpd.GeoSeries([], crs=cells.crs))

    geometries = members.geometry.to_numpy()
    patch_of_cell = _label_connected(geometries)

    rows: list[dict[str, object]] = []
    outlines: list[BaseGeometry] = []
    for label in np.unique(patch_of_cell):
        in_patch = geometries[patch_of_cell == label]
        outlines.append(_alpha_outline(in_patch, alpha_radius_m))
        rows.append(
            {
                'n_cells': len(in_patch),
                'area_km2': float(np.sum(shapely.area(in_patch))) / 1e6,  # pyright: ignore[reportUnknownMemberType] -- shapely.area stub has untyped **kwargs
                'geometry': shapely.union_all(in_patch),
            }
        )
    outline_series = gpd.GeoSeries(outlines, crs=cells.crs)
    patches = gpd.GeoDataFrame(rows, geometry='geometry', crs=cells.crs)
    patches['outline'] = outline_series
    patches['elongation'] = compute_elongation(outline_series)
    patches = patches.sort_values('area_km2', ascending=False).reset_index(drop=True)
    patches['patch_id'] = patches.index
    return gpd.GeoDataFrame(patches[PATCH_COLUMNS], geometry='geometry', crs=cells.crs)


def add_road_measures(
    patches: gpd.GeoDataFrame,
    road: BaseGeometry,
    *,
    near_m: float,
    cover_radius_m: float = 250.0,
) -> gpd.GeoDataFrame:
    """Copy of `patches` with how much of `road` each lines and how close it sits.

    `road_cover_m` is the road length inside the patch, measured as
    area(patch ∩ road buffered by r) / 2r. For a line of length L that buffer has
    area 2rL + πr² (Steiner's formula), so away from the road's two ends this is L.
    Unlike summing line length, it does not double-count divided roads mapped as
    two parallel one-way ways: carriageways d apart share one buffer of width
    2r + d, an overestimate of d / 2r (4% for d = 20 m at r = 250 m). It assumes
    bends are gentler than radius r, and undercounts road running along a patch's
    edge rather than through it.

    Args:
        patches: Output of `detect_patches`, in a projected CRS.
        road: The road's centrelines in the same CRS (e.g. a route's union_all()).
        near_m: Distance from the road counted as "near" for `near_road_frac`.
            Make it at least one cell wide, or a one-cell strip on the road can
            never be fully near it.
        cover_radius_m: Buffer radius r for `road_cover_m`.

    Returns:
        A new GeoDataFrame with `road_cover_m` (m) and `near_road_frac` (0-1, share
        of patch area within `near_m` of the road) appended.
    """
    geometries = patches.geometry
    road_cover_m = geometries.intersection(road.buffer(cover_radius_m)).area / (2 * cover_radius_m)
    near_road_frac = geometries.intersection(road.buffer(near_m)).area / geometries.area
    return patches.assign(road_cover_m=road_cover_m, near_road_frac=near_road_frac)


def compute_elongation(outlines: gpd.GeoSeries) -> pd.Series:
    """Long side / short side of each outline's equivalent rectangle (see module docstring).

    Holes are filled first: a courtyard inside a patch should not count as perimeter.

    Args:
        outlines: Polygons in a projected CRS.

    Returns:
        Elongation >= 1 per outline, indexed like `outlines`.
    """
    filled = outlines.map(_fill_holes)
    quarter_perimeter = filled.length / 4
    discriminant = quarter_perimeter**2 - filled.area
    root = np.sqrt(discriminant.clip(lower=0))
    long_side, short_side = quarter_perimeter + root, quarter_perimeter - root
    elongation = (long_side / short_side).where(discriminant > 0, 1.0)
    return elongation.astype(float).rename('elongation')


def _label_connected(geometries: NDArray[np.object_]) -> NDArray[np.intp]:
    """Connected-component label per geometry; touching (within tolerance) = connected."""
    merged = shapely.union_all(shapely.buffer(geometries, _ADJACENCY_TOLERANCE_M))
    parts = shapely.get_parts(merged)
    tree = shapely.STRtree(parts)
    probe = shapely.point_on_surface(geometries)
    cell_pos, part_pos = tree.query(probe, predicate='within')
    labels = np.full(len(geometries), -1, dtype=np.intp)
    labels[cell_pos] = part_pos
    return labels


def _alpha_outline(geometries: NDArray[np.object_], radius_m: float) -> BaseGeometry:
    vertices = np.unique(shapely.get_coordinates(geometries).round(3), axis=0)
    if len(vertices) < 4:  # too few for a triangulation; nothing to smooth
        return shapely.union_all(geometries).convex_hull
    shapes: Any = alpha_shape(vertices, 1.0 / radius_m)  # pyright: ignore[reportUnknownVariableType] -- libpysal is untyped
    return shapely.union_all(np.asarray(shapes.to_numpy()))


def _fill_holes(geometry: BaseGeometry) -> BaseGeometry:
    shells = [
        Polygon(part.exterior)
        for part in shapely.get_parts(geometry)
        if isinstance(part, Polygon) and not math.isnan(part.area)
    ]
    return shells[0] if len(shells) == 1 else MultiPolygon(shells)
