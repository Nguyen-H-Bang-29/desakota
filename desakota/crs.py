"""Named coordinate reference systems.

Three CRSs are in concurrent use, so there is deliberately no bare `CRS`
constant: every reprojection must say which of these it is going to, and why.
"""

from typing import Final

CRS_WGS84: Final[str] = 'EPSG:4326'
"""Geographic lon/lat (degrees). Required by Overpass, STAC `bbox`, and folium."""

CRS_UTM: Final[str] = 'EPSG:32648'
"""WGS 84 / UTM zone 48N (metres). Working CRS: the LULC raster's native grid."""

CRS_EQUAL_AREA: Final[str] = 'EPSG:6933'
"""WGS 84 / NSIDC EASE-Grid 2.0 Global (Lambert cylindrical equal-area, metres).

Use for absolute areas; not needed for within-polygon ratios in `CRS_UTM`.
"""
