"""Tests for the H3 hexagon grid builder."""

import h3
import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon, box

from desakota.crs import CRS_UTM, CRS_WGS84
from desakota.hexgrid import build_hex_grid


# h3-py ships no type hints; these typed shims keep the untyped surface in one place.
def _is_cell_at(hex_id: str, resolution: int) -> bool:
    is_valid = bool(h3.is_valid_cell(hex_id))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] -- untyped h3
    return is_valid and int(h3.get_resolution(hex_id)) == resolution  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] -- untyped h3


def _cell_centre_lat_lng(hex_id: str) -> tuple[float, float]:
    lat, lng = h3.cell_to_latlng(hex_id)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- untyped h3
    return float(lat), float(lng)  # pyright: ignore[reportUnknownArgumentType] -- untyped h3


def _hexagon_area_m2(resolution: int) -> float:
    return float(h3.average_hexagon_area(resolution, 'm^2'))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] -- untyped h3


def _n_vertices(geometry: object) -> int:
    assert isinstance(geometry, Polygon)
    return len(geometry.exterior.coords) - 1


# 3 km x 2 km rectangle near Bac Ninh, EPSG:32648 metres.
FOOTPRINT = box(600_000, 2_340_000, 603_000, 2_342_000)


def test_grid_is_in_the_footprint_crs_with_valid_unique_cells() -> None:
    hexes = build_hex_grid(FOOTPRINT, crs=CRS_UTM, resolution=9)

    assert hexes.crs == CRS_UTM
    assert list(hexes.columns) == ['hex_id', 'geometry']
    assert hexes['hex_id'].is_unique
    assert all(_is_cell_at(c, 9) for c in hexes['hex_id'])


def test_grid_tiles_the_footprint_exactly() -> None:
    hexes = build_hex_grid(FOOTPRINT, crs=CRS_UTM, resolution=9)

    # Clipped to the footprint, no gaps, no overlaps: areas add up.
    assert hexes.union_all().symmetric_difference(FOOTPRINT).area < 1.0  # m2
    assert hexes.area.sum() == pytest.approx(FOOTPRINT.area, rel=1e-6)


def test_interior_cells_are_whole_hexagons_of_the_expected_size() -> None:
    hexes = build_hex_grid(FOOTPRINT, crs=CRS_UTM, resolution=9)

    interior = hexes[hexes.within(FOOTPRINT.buffer(-500))]
    assert len(interior) > 0
    assert all(_n_vertices(g) == 6 for g in interior.geometry)
    # H3 res 9 cells are ~0.105 km2; UTM scale error here is < 0.1%.
    assert interior.area.median() == pytest.approx(_hexagon_area_m2(9), rel=0.1)


def test_cells_sit_where_h3_says_they_are() -> None:
    # Guards the (lat, lng) order h3 returns; swapped axes would land in the sea.
    hexes = build_hex_grid(FOOTPRINT, crs=CRS_UTM, resolution=9)
    to_utm = Transformer.from_crs(CRS_WGS84, CRS_UTM, always_xy=True)

    interior = hexes[hexes.within(FOOTPRINT.buffer(-500))]
    for hex_id, geometry in zip(interior['hex_id'], interior.geometry, strict=True):
        lat, lng = _cell_centre_lat_lng(hex_id)
        assert geometry.contains(Point(to_utm.transform(lng, lat)))


def test_coarser_resolution_gives_fewer_cells() -> None:
    n_res8 = len(build_hex_grid(FOOTPRINT, crs=CRS_UTM, resolution=8))
    n_res9 = len(build_hex_grid(FOOTPRINT, crs=CRS_UTM, resolution=9))

    assert n_res8 < n_res9


def test_multipart_footprint_is_covered() -> None:
    footprint = MultiPolygon([FOOTPRINT, box(610_000, 2_340_000, 611_000, 2_341_000)])

    hexes = build_hex_grid(footprint, crs=CRS_UTM, resolution=9)

    assert hexes.area.sum() == pytest.approx(footprint.area, rel=1e-6)


def test_geographic_crs_is_rejected() -> None:
    with pytest.raises(ValueError, match='projected'):
        build_hex_grid(box(106.0, 21.0, 106.1, 21.1), crs=CRS_WGS84, resolution=9)


def test_empty_footprint_is_rejected() -> None:
    with pytest.raises(ValueError, match='empty'):
        build_hex_grid(box(0, 0, 0, 0), crs=CRS_UTM, resolution=9)
