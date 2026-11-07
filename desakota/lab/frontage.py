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
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, cast

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import rasterio
import shapely
from numpy.typing import NDArray
from rasterio.features import (
    rasterize,  # pyright: ignore[reportUnknownVariableType] -- rasterio stubs are incomplete
)
from rasterio.windows import Window
from shapely.geometry import LineString, box
from shapely.geometry.base import BaseGeometry

from desakota.landcover import require_inside_raster, snapped_raster_window

ROAD_PIECE_COLUMNS: Final = ['piece_id', 'chainage_m', 'station_id', 'direction_sign', 'geometry']

RIBBON_RUN_COLUMNS: Final = ['side', 'first_station', 'n_stations', 'length_m', 'start_km']

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
    `gap_penalty` = 1 the path cuts every corner it can.

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


def detect_ribbon_runs(
    station_class: pd.Series,
    *,
    ribbon_label: str = 'ribbon',
    station_m: float,
    min_length_m: float,
) -> pd.DataFrame:
    """Find runs of consecutive ribbon-classed stations per side.

    A run is a maximal block of stations on the same side whose class equals
    `ribbon_label`. Following Verbeek, Boussauw & Pisman (2014), a ribbon is at
    least a given length long; shorter runs are dropped.

    Args:
        station_class: Per-station class, indexed by a MultiIndex with levels
            `station_id` (integer, dense per side) and `side` (string). This is
            the long-form classification produced in notebook 05.
        ribbon_label: Class value that counts as a ribbon station.
        station_m: Length of a station in metres; the station's run contributes
            `station_m` to the ribbon's length.
        min_length_m: Runs shorter than this are dropped.

    Returns:
        One row per surviving run, columns `side`, `first_station`,
        `n_stations`, `length_m` and `start_km`. Empty with these columns when
        nothing qualifies.

    Raises:
        ValueError: If `station_class` is not indexed by `(station_id, side)`.
    """
    if list(station_class.index.names) != ['station_id', 'side']:
        raise ValueError(
            "station_class must have MultiIndex levels ['station_id', 'side']; "
            f'got {list(station_class.index.names)}'
        )
    runs: list[dict[str, Any]] = []
    for side, classes in station_class.groupby(level='side'):
        is_ribbon = classes.droplevel('side').sort_index() == ribbon_label
        run_id = (is_ribbon != is_ribbon.shift()).cumsum()
        for _, run in is_ribbon[is_ribbon].groupby(run_id[is_ribbon]):
            runs.append(
                {'side': side, 'first_station': int(run.index.min()), 'n_stations': len(run)}
            )
    ribbons = pd.DataFrame(runs, columns=['side', 'first_station', 'n_stations'])
    ribbons['length_m'] = ribbons['n_stations'] * station_m
    ribbons['start_km'] = ribbons['first_station'] * station_m / 1000
    return ribbons[ribbons['length_m'] >= min_length_m].reset_index(drop=True)[RIBBON_RUN_COLUMNS]


_ZONE_SIMPLIFY_M: Final[float] = 20.0
"""Tolerance for the road outline that bounds `compute_frontage_counts`'s pixel search."""

_STRIP_ROWS: Final[int] = 512
"""Rows read per strip; bounds peak memory for the per-pixel nearest-piece pass."""


def _open(fp: str | Path, mode: str = 'r', **profile: Any) -> Any:
    """`rasterio.open`, typed as `Any`: absorbs rasterio's unknown C-extension stub types."""
    return rasterio.open(fp, mode, **profile)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- rasterio stubs are incomplete


FRONTAGE_COUNT_COLUMNS: Final = [
    'station_id',
    'side',
    'depth_min_m',
    'depth_max_m',
    'n_valid_px',
    'n_built_px',
]


def compute_frontage_counts(
    pieces: gpd.GeoDataFrame,
    raster_path: str | Path,
    legend: dict[int, str],
    built_area_label: str,
    water_label: str,
    *,
    depth_edges_m: Sequence[float],
    exclude: BaseGeometry | None = None,
) -> pd.DataFrame:
    """Count built and non-water pixels per station, road side and distance band.

    Every pixel whose centre lies within `depth_edges_m[-1]` of a road piece, and
    outside `exclude`, is assigned to its **nearest piece**, which gives it a
    station, a distance and a side: left or right of the reference line's
    direction (the piece's own left, flipped by its `direction_sign`). Assigning by
    nearest piece partitions the pixels, so nothing is counted twice on bends or
    between the two carriageways of a divided road, and pixels near a gap the
    reference line jumps go to the mapped road on either side of it. A pixel
    exactly on a piece's line counts as left.

    Args:
        pieces: Output of `build_road_pieces`, in the raster's CRS.
        raster_path: Categorical land-cover raster, e.g. from `fetch_landcover`.
        legend: Pixel value -> class name, from `get_landcover_legend`.
        built_area_label: Class counted as built, e.g. `"Built area"`.
        water_label: Class left out of `n_valid_px`, e.g. `"Water"`.
        depth_edges_m: Increasing distance-band edges from the road centreline in
            metres, e.g. (0, 30, 60, 100, 250, 500, 1000, 1500).
        exclude: Area left out entirely, e.g. the road surface (centrelines
            buffered by half the carriageway width), which the LULC maps as built.

    Returns:
        Long table with one row per (station, side, band) holding at least one
        valid pixel: `station_id`, `side` (`"left"`/`"right"`), `depth_min_m`,
        `depth_max_m`, `n_valid_px` (non-water, non-nodata) and `n_built_px`.
        Keep counts rather than fractions so stations and bands can be pooled
        before dividing.

    Raises:
        ValueError: If the edges are not increasing, the CRS does not match the
            raster's, or the pieces' surroundings extend beyond it.
    """
    edges_m = np.asarray(depth_edges_m, dtype=np.float64)
    if len(edges_m) < 2 or np.any(np.diff(edges_m) <= 0) or edges_m[0] < 0:
        raise ValueError('depth_edges_m must be at least two increasing, non-negative values.')
    max_depth_m = float(edges_m[-1])
    n_bands = len(edges_m) - 1
    value_of = {label: value for value, label in legend.items()}
    built_value, water_value = value_of[built_area_label], value_of[water_label]

    geometries = pieces.geometry.to_numpy()
    station_of_piece = pieces['station_id'].to_numpy(dtype=np.int64)
    sign_of_piece = pieces['direction_sign'].to_numpy(dtype=np.int64)
    endpoints = shapely.get_coordinates(geometries).reshape(-1, 2, 2)  # two-point pieces
    start_x, start_y = endpoints[:, 0, 0], endpoints[:, 0, 1]
    end_x, end_y = endpoints[:, 1, 0], endpoints[:, 1, 1]
    tree = shapely.STRtree(geometries)
    # The zone only pre-selects candidate pixels (query_nearest's max_distance makes the
    # exact cut), so a simplified line buffered by the tolerance more is enough, and
    # buffering the raw union of ~10^4 pieces by kilometres exhausts GEOS's memory.
    merged_road = shapely.line_merge(shapely.union_all(geometries))  # pyright: ignore[reportUnknownMemberType] -- shapely stubs leave **kwargs untyped
    simple_road = shapely.simplify(merged_road, _ZONE_SIMPLIFY_M)
    zone = shapely.buffer(simple_road, max_depth_m + _ZONE_SIMPLIFY_M, quad_segs=4)
    zone_shapes: list[tuple[BaseGeometry, int]] = [(zone, 1)]
    if exclude is not None and not exclude.is_empty:
        zone_shapes.append((exclude, 0))  # later shapes overwrite earlier ones
    # The raster must cover every point within max_depth_m of a piece (not the padded zone).
    minx, miny, maxx, maxy = (float(v) for v in pieces.total_bounds)
    reach_box = box(minx - max_depth_m, miny - max_depth_m, maxx + max_depth_m, maxy + max_depth_m)
    reach = gpd.GeoDataFrame(geometry=[reach_box], crs=pieces.crs)

    n_keys = (int(station_of_piece.max()) + 1) * 2 * n_bands
    valid_px = np.zeros(n_keys, dtype=np.int64)
    built_px = np.zeros(n_keys, dtype=np.int64)

    with _open(raster_path) as src:
        require_inside_raster(reach, src)
        nodata: int = int(src.nodata) if src.nodata is not None else 0
        col_off, row_off, width, height = snapped_raster_window(reach.total_bounds, src)
        for row in range(row_off, row_off + height, _STRIP_ROWS):
            n_rows = min(_STRIP_ROWS, row_off + height - row)
            strip = Window(col_off, row, width, n_rows)  # pyright: ignore[reportCallIssue] -- Window is an attrs class the stubs leave untyped
            strip_transform: Any = src.window_transform(strip)
            in_zone = np.asarray(
                rasterize(  # pyright: ignore[reportUnknownArgumentType] -- rasterio stubs are incomplete
                    zone_shapes,
                    out_shape=(n_rows, width),
                    transform=strip_transform,
                    fill=0,
                    dtype='uint8',
                ),
                dtype=bool,
            )
            if not in_zone.any():
                continue
            lulc = np.asarray(src.read(1, window=strip), dtype=np.uint8)
            rows, cols = np.nonzero(in_zone & (lulc != nodata) & (lulc != water_value))
            if len(rows) == 0:
                continue
            px_x: NDArray[np.float64] = strip_transform.c + (cols + 0.5) * strip_transform.a
            px_y: NDArray[np.float64] = strip_transform.f + (rows + 0.5) * strip_transform.e
            (px_idx, piece_idx), dist_m = tree.query_nearest(
                shapely.points(px_x, px_y),
                max_distance=max_depth_m,
                return_distance=True,
                all_matches=False,
            )
            band = np.searchsorted(edges_m, dist_m, side='right') - 1
            keep = (band >= 0) & (band < n_bands)
            px_idx, piece_idx, band = px_idx[keep], piece_idx[keep], band[keep]
            cross = (end_x[piece_idx] - start_x[piece_idx]) * (
                px_y[px_idx] - start_y[piece_idx]
            ) - (end_y[piece_idx] - start_y[piece_idx]) * (px_x[px_idx] - start_x[piece_idx])
            is_right = (cross * sign_of_piece[piece_idx]) < 0
            key = (station_of_piece[piece_idx] * 2 + is_right) * n_bands + band
            is_built = lulc[rows[px_idx], cols[px_idx]] == built_value
            valid_px += np.bincount(key, minlength=n_keys)
            built_px += np.bincount(key[is_built], minlength=n_keys)

    (observed,) = np.nonzero(valid_px)
    band_of_key = observed % n_bands
    return pd.DataFrame(
        {
            'station_id': observed // (2 * n_bands),
            'side': np.where((observed // n_bands) % 2 == 1, 'right', 'left'),
            'depth_min_m': edges_m[band_of_key],
            'depth_max_m': edges_m[band_of_key + 1],
            'n_valid_px': valid_px[observed],
            'n_built_px': built_px[observed],
        }
    )[FRONTAGE_COUNT_COLUMNS]


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
