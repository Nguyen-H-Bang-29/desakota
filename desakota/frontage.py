"""Linear referencing along a mapped road: a chainage axis and short road pieces.

A frontage profile reports land cover per stretch of road ("km 42-43, left side"),
so every pixel needs a position along the road. OSM maps QL18 as hundreds of ways:
divided sections are two one-way chains, and stretches co-signed with an
expressway are missing from the national tier, so there is no single line to
measure along.

Two layers solve this, with separate jobs:

- The **reference line** (`build_reference_line`) only supplies the chainage axis.
  It is the shortest path through the noded road network from one end to the
  other, with straight jumps across gaps that cost `gap_penalty` times their
  length, so the path prefers mapped road and takes one carriageway where there
  are two. Errors in it shift chainage slightly; they never move a pixel.
- **Road pieces** (`build_road_pieces`) are the mapped road cut into straight
  segments of at most `piece_m`. Pixels are assigned to their nearest piece, so
  both carriageways of a divided road get their own outer frontage, and stretches
  where the reference line jumps a gap get no pixels at all. Each piece carries
  the chainage of its midpoint on the reference line and `direction_sign`, +1
  where it runs the same way as the reference and -1 against it. One-way pieces
  in opposite directions therefore still agree on which side is "left".

All geometry is in the caller's projected CRS (metres); nothing is reprojected.
"""

import math
from itertools import pairwise
from typing import Any, Final, cast

import geopandas as gpd
import networkx as nx
import numpy as np
import shapely
from numpy.typing import NDArray
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry

ROAD_PIECE_COLUMNS: Final = ['piece_id', 'chainage_m', 'station_id', 'direction_sign', 'geometry']

_NODE_DECIMALS: Final = 1
"""Endpoints are matched after rounding to 0.1 m, which absorbs float noise from noding."""

_TANGENT_HALF_STEP_M: Final = 5.0
"""Half the chord used to estimate the reference line's direction at a chainage."""

type _Node = tuple[float, float]


def build_reference_line(
    road: BaseGeometry, *, max_gap_m: float = 5_000.0, gap_penalty: float = 5.0
) -> LineString:
    """A single line through `road` from one end to the other, for chainage only.

    The road is noded (split wherever lines cross or touch), each piece becomes a
    graph edge weighted by its length, and every pair of nodes in different
    connected components within `max_gap_m` gets a straight "jump" edge weighted by
    `gap_penalty` x its length. The ends are the two dangling nodes (degree 1)
    farthest apart, and the result runs from the western one (smaller x) to the
    other.

    The penalty is a trade-off. A jump of length g still wins over mapped road of
    length L between the same two points whenever g x penalty < L, so a road that
    loops back near itself (a U-turn, a detour through a town) can be skipped. With
    `gap_penalty` = 1 the path cuts every corner it can; on QL18 a penalty of 5 kept
    278 km of mapped road with 4 km of jumps. Check the result on a map.

    Args:
        road: Road centrelines (LineString or MultiLineString) in a projected CRS.
        max_gap_m: Longest jump allowed between disconnected parts of the road.
        gap_penalty: Cost per metre of a jump relative to a metre of mapped road.

    Returns:
        The path as one LineString, starting at the western end.

    Raises:
        ValueError: If the road is empty, or its parts can't be joined into one
            path with jumps of at most `max_gap_m`.
    """
    lines = [
        part
        for part in shapely.get_parts(shapely.node(road))
        if isinstance(part, LineString) and part.length > 0
    ]
    if not lines:
        raise ValueError('road has no line geometry.')

    graph: nx.Graph[_Node] = nx.Graph()
    for line in lines:
        start, end = _node(line.coords[0]), _node(line.coords[-1])
        if start == end:  # closed ring (e.g. a roundabout) adds no reachability
            continue
        current = cast(dict[str, Any] | None, graph.get_edge_data(start, end))
        if current is None or line.length < current['length_m']:
            graph.add_edge(start, end, length_m=line.length, cost=line.length, line=line)
    _add_gap_jumps(graph, max_gap_m=max_gap_m, gap_penalty=gap_penalty)

    west, east = _farthest_dangling_pair(graph)
    if not nx.has_path(graph, west, east):
        raise ValueError(f'road parts cannot be joined with jumps of at most {max_gap_m} m.')
    path: list[_Node] = nx.shortest_path(graph, west, east, weight='cost')  # pyright: ignore[reportUnknownMemberType] -- networkx stubs leave **backend_kwargs untyped

    coords: list[tuple[float, float]] = [path[0]]
    for start, end in pairwise(path):
        line = cast(LineString | None, graph.edges[start, end]['line'])
        if line is None:  # a jump: straight across
            coords.append(end)
            continue
        edge_coords = [(x, y) for x, y, *_ in line.coords]
        if _node(edge_coords[0]) != start:
            edge_coords.reverse()
        coords.extend(edge_coords[1:])
    return LineString(coords)


def build_road_pieces(
    road: BaseGeometry, reference: LineString, *, piece_m: float, station_m: float
) -> gpd.GeoDataFrame:
    """Cut `road` into straight pieces and place each on the reference line's chainage.

    Args:
        road: Road centrelines in a projected CRS; every carriageway is kept.
        reference: Chainage axis from `build_reference_line`, same CRS.
        piece_m: Longest piece. Pieces are straight two-point segments, so the side
            of a pixel is exact; 20 m keeps them straight on any real bend.
        station_m: Length of a station, the unit of the profile.
            `station_id` = floor(chainage_m / station_m).

    Returns:
        One row per piece: `piece_id`, `chainage_m` (of the piece's midpoint,
        projected on `reference`), `station_id`, `direction_sign` (+1 along the
        reference, -1 against) and `geometry` (a two-point LineString). No CRS is
        attached; set it from the caller's layer.

    Raises:
        ValueError: If `piece_m` or `station_m` is not positive.
    """
    if piece_m <= 0 or station_m <= 0:
        raise ValueError('piece_m and station_m must be positive.')
    start_parts: list[NDArray[np.float64]] = []
    end_parts: list[NDArray[np.float64]] = []
    for line in shapely.get_parts(shapely.segmentize(road, piece_m)):
        coords = np.asarray(line.coords, dtype=np.float64)[:, :2]
        start_parts.append(coords[:-1])
        end_parts.append(coords[1:])
    starts, ends = np.concatenate(start_parts), np.concatenate(end_parts)
    is_real = np.any(starts != ends, axis=1)
    starts, ends = starts[is_real], ends[is_real]

    chainage_m = _locate_on(reference, (starts + ends) / 2)
    ahead = _point_at(reference, chainage_m + _TANGENT_HALF_STEP_M)
    behind = _point_at(reference, chainage_m - _TANGENT_HALF_STEP_M)
    along = np.sum((ends - starts) * (ahead - behind), axis=1)
    direction_sign = np.where(along >= 0, 1, -1)

    return gpd.GeoDataFrame(
        {
            'piece_id': np.arange(len(starts)),
            'chainage_m': chainage_m,
            'station_id': np.floor(chainage_m / station_m).astype(np.int64),
            'direction_sign': direction_sign.astype(np.int8),
        },
        geometry=shapely.linestrings(np.stack([starts, ends], axis=1)),
    )[ROAD_PIECE_COLUMNS]


def _locate_on(line: LineString, xy: NDArray[np.float64]) -> NDArray[np.float64]:
    """Distance along `line` of the closest point to each (x, y) row."""
    located = shapely.line_locate_point(line, shapely.points(xy))  # pyright: ignore[reportUnknownMemberType] -- shapely stubs leave **kwargs untyped
    return np.asarray(located, dtype=np.float64)


def _point_at(line: LineString, distance_m: NDArray[np.float64]) -> NDArray[np.float64]:
    """(x, y) rows of the points at each distance along `line`, clamped to its ends.

    The clamp matters: shapely reads a negative distance as measured from the end.
    """
    points = shapely.line_interpolate_point(line, np.clip(distance_m, 0.0, line.length))  # pyright: ignore[reportUnknownMemberType] -- shapely stubs leave **kwargs untyped
    return shapely.get_coordinates(points)


def _node(coord: tuple[float, ...]) -> _Node:
    return (round(coord[0], _NODE_DECIMALS), round(coord[1], _NODE_DECIMALS))


def _add_gap_jumps(graph: nx.Graph[_Node], *, max_gap_m: float, gap_penalty: float) -> None:
    """Add straight edges between nodes of different components within `max_gap_m`."""
    nodes: list[_Node] = list(graph.nodes)
    component_of = {
        node: label
        for label, component in enumerate(nx.connected_components(graph))
        for node in component
    }
    points = cast(NDArray[np.object_], shapely.points(np.asarray(nodes, dtype=np.float64)))
    pairs = np.asarray(
        shapely.STRtree(points).query(points, predicate='dwithin', distance=max_gap_m),
        dtype=np.intp,
    )
    for i, j in zip(pairs[0], pairs[1], strict=True):
        a, b = nodes[int(i)], nodes[int(j)]
        if i < j and component_of[a] != component_of[b]:
            gap_m = math.dist(a, b)
            graph.add_edge(a, b, length_m=gap_m, cost=gap_penalty * gap_m, line=None)


def _farthest_dangling_pair(graph: nx.Graph[_Node]) -> tuple[_Node, _Node]:
    """The two degree-1 nodes of the mapped road farthest apart, western one first."""
    dangling = [node for node in graph.nodes if _mapped_degree(graph, node) == 1]
    if len(dangling) < 2:
        dangling = list(graph.nodes)
    xy = np.asarray(dangling, dtype=np.float64)
    gaps = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    flat = int(np.argmax(gaps))
    a, b = dangling[flat // len(dangling)], dangling[flat % len(dangling)]
    return (a, b) if a[0] <= b[0] else (b, a)


def _mapped_degree(graph: nx.Graph[_Node], node: _Node) -> int:
    """Degree counting only mapped-road edges, not gap jumps."""
    return sum(
        1 for neighbour in graph.neighbors(node) if graph.edges[node, neighbour]['line'] is not None
    )
