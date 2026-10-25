"""H3 hexagon grids over a study area, for ESDA on regular units instead of communes.

Why hexagons: every neighbour's centroid is equidistant and every cell has six
neighbours, so Queen and Rook contiguity coincide and a LISA is not biased by the
diagonal neighbours a square grid has. Why H3 (Brodsky 2018, https://h3geo.org):
stable, hierarchical cell ids at fixed resolutions, so res 8/9/10 are nested
(approximately; H3 children do not tile their parent exactly) and results can be
joined across runs. H3 cells are not exactly equal-area: over the QL18 corridor,
whole cells measured in EPSG:32648 vary by 2-3% in area. `built_frac` is a ratio,
so this doesn't bias it; anything summed per cell should divide by the cell's area.

h3 works in WGS 84 and returns boundaries as (lat, lng); everything crossing this
module's boundary is in the caller's projected CRS.
"""

from collections.abc import Iterable
from typing import cast

import geopandas as gpd
import h3
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.ops import transform

from desakota.crs import CRS_WGS84


def build_hex_grid(
    footprint: Polygon | MultiPolygon, *, crs: str, resolution: int
) -> gpd.GeoDataFrame:
    """Tile `footprint` with H3 cells, clipped to it.

    Cells are selected by centre on the footprint grown by one hexagon edge, which
    guarantees full coverage, then intersected with the footprint. Edge cells are
    therefore partial hexagons; the cells partition the footprint exactly (no gaps,
    no overlaps), so per-cell pixel counts add up to the footprint's.

    Args:
        footprint: Study area in `crs`.
        crs: A projected CRS (metres), e.g. `CRS_UTM`.
        resolution: H3 resolution; 8 ~ 0.74 km2, 9 ~ 0.105 km2, 10 ~ 0.015 km2.

    Returns:
        One row per cell, columns `hex_id` (H3 index, str) and `geometry`, in `crs`.

    Raises:
        ValueError: If `crs` is not projected or the footprint is empty.
    """
    to_wgs84 = Transformer.from_crs(crs, CRS_WGS84, always_xy=True)
    if not to_wgs84.source_crs or not to_wgs84.source_crs.is_projected:
        raise ValueError(f'footprint CRS must be projected (metres); got {crs}.')
    if footprint.is_empty or footprint.area == 0:
        raise ValueError('footprint is empty.')

    edge_m = cast(float, h3.average_hexagon_edge_length(resolution, 'm'))  # pyright: ignore[reportUnknownMemberType] -- h3-py ships no type hints
    grown_wgs84 = transform(to_wgs84.transform, footprint.buffer(edge_m))
    shape = h3.geo_to_h3shape(mapping(grown_wgs84))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- h3-py ships no type hints
    hex_ids = sorted(cast(Iterable[str], h3.h3shape_to_cells(shape, resolution)))  # pyright: ignore[reportUnknownMemberType] -- h3-py ships no type hints

    to_crs = Transformer.from_crs(CRS_WGS84, crs, always_xy=True)
    cells = [transform(to_crs.transform, _cell_polygon_wgs84(hex_id)) for hex_id in hex_ids]
    hexes = gpd.GeoDataFrame({'hex_id': hex_ids}, geometry=cells, crs=crs)
    clipped = hexes.assign(geometry=hexes.intersection(footprint))
    return clipped[~clipped.is_empty & (clipped.area > 0)].reset_index(drop=True)


def _cell_polygon_wgs84(hex_id: str) -> Polygon:
    boundary = cast(Iterable[tuple[float, float]], h3.cell_to_boundary(hex_id))  # pyright: ignore[reportUnknownMemberType] -- h3-py ships no type hints
    return Polygon([(lng, lat) for lat, lng in boundary])  # h3 gives (lat, lng)
