"""Support code for exploring urbanization and sprawl in the Red River Delta.

Notebooks under `notebooks/` are the exploratory surface; anything that earns a
test or gets reused across notebooks belongs here.
"""

from desakota.landcover import compute_built_frac, fetch_landcover, get_landcover_legend
from desakota.paths import DataLayout
from desakota.stac import (
    COLLECTIONS,
    create_client,
    get_bbox_from_aoi,
    get_collection,
    search_items,
)

__all__ = [
    'COLLECTIONS',
    'DataLayout',
    'compute_built_frac',
    'create_client',
    'fetch_landcover',
    'get_bbox_from_aoi',
    'get_collection',
    'get_landcover_legend',
    'search_items',
]
