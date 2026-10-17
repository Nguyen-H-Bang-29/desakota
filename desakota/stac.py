"""Planetary Computer STAC access for the annual land-cover collection.

Centralizes the collection registry and search helpers so notebooks don't
each hand-roll client setup and bbox/datetime plumbing.
"""

from __future__ import annotations

from typing import Final

import geopandas as gpd
import planetary_computer
import pystac
import pystac_client

STAC_API_URL: Final[str] = 'https://planetarycomputer.microsoft.com/api/stac/v1'

COLLECTIONS: Final[dict[str, str]] = {'10M_ANNUAL_LANDUSE': 'io-lulc-annual-v02'}
"""Short registry name -> Planetary Computer collection id."""


def create_client(stac_api_url: str = STAC_API_URL) -> pystac_client.Client:
    """Open a Planetary Computer STAC client with automatic asset signing.

    Args:
        stac_api_url: Root URL of the STAC API to connect to.

    Returns:
        An open STAC client; asset hrefs it returns are pre-signed, so they
        can be read directly (e.g. by `rasterio`/`rioxarray`) without a
        separate signing step.
    """
    return pystac_client.Client.open(stac_api_url, modifier=planetary_computer.sign_inplace)


def get_collection(
    client: pystac_client.Client, collection_name: str
) -> pystac_client.CollectionClient | pystac.Collection:
    """Look up a STAC collection by its short registry name.

    Args:
        client: An open STAC client.
        collection_name: Key into `COLLECTIONS`, e.g. `"10M_ANNUAL_LANDUSE"`.

    Returns:
        The matching STAC collection. `pystac_client` types this as either a
        `CollectionClient` or a plain `pystac.Collection` depending on how
        the catalog was opened; both expose the `.id`/`.item_assets` this
        module uses.

    Raises:
        ValueError: If `collection_name` is not a key in `COLLECTIONS`.
    """
    if collection_name not in COLLECTIONS:
        raise ValueError(f"Collection '{collection_name}' not found in COLLECTIONS.")
    return client.get_collection(collection_id=COLLECTIONS[collection_name])


def get_bbox_from_aoi(aoi: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    """Compute the bounding box of an AOI in its current CRS.

    Args:
        aoi: Area-of-interest polygons. Reproject to WGS84 first if the bbox
            is going to a STAC search — the API expects lon/lat.

    Returns:
        `(minx, miny, maxx, maxy)`, matching `GeoDataFrame.total_bounds` order.
    """
    minx, miny, maxx, maxy = aoi.total_bounds
    return (minx, miny, maxx, maxy)


def search_items(
    client: pystac_client.Client,
    collection: pystac_client.CollectionClient | pystac.Collection,
    bbox: tuple[float, float, float, float],
    datetime: str,
) -> pystac_client.ItemSearch:
    """Search a STAC collection for items intersecting a bbox and time range.

    Args:
        client: An open STAC client.
        collection: The collection to search; only its `.id` is used.
        bbox: `(minx, miny, maxx, maxy)` in WGS84 lon/lat.
        datetime: STAC datetime range string, e.g. `"2023-01-01/2023-12-31"`.

    Returns:
        A lazy `ItemSearch` — call `.items()` or `.item_collection()` on it
        to fetch results.
    """
    return client.search(collections=[collection.id], bbox=list(bbox), datetime=datetime)
