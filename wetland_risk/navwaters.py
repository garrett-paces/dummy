"""Navigable-waters proxy via USGS National Hydrography Dataset.

The Corps' Navigable Waters List is not published as a single spatial
feature service. In practice, the strongest publicly available proxy
for "relatively permanent waters" (RPW) and traditional navigable
waters (TNW) is the NHD flowline layer filtered by:

  * FCode 46006 (perennial streams)
  * Named large rivers (GNIS_NAME populated, and flowlines that
    connect to the sea or to Great Lakes)

We use USGS NHDPlus HR MapServer, layer for NHDFlowline:

    https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/NHDPlus_HR/MapServer/3

For the risk model we care about only two questions:

    1. Is there a perennial flowline inside the AOI?
    2. What is the distance from the AOI to the nearest perennial
       flowline outside the AOI (within a search buffer)?

Sackett requires a "continuous surface connection" — a small distance
here is a leading indicator that the wetland is likely adjacent to a
water of the U.S.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from ._http import get_json
from .aoi import AOI, haversine_m, min_distance_to_line_m

log = logging.getLogger(__name__)

NHD_FLOWLINE_URL = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR"
    "/NHDPlus_HR/MapServer/3/query"
)

# FCodes: https://www.usgs.gov/national-hydrography/feature-codes
FCODE_PERENNIAL_STREAM = 46006
FCODE_ARTIFICIAL_PATH = 55800  # inside lakes/estuaries, treated as connected water
FCODES_PERMANENT = (FCODE_PERENNIAL_STREAM, FCODE_ARTIFICIAL_PATH)

# Search radius when looking OUTSIDE the AOI (meters). Beyond ~500 ft
# (~150 m) the adjacency argument gets much harder to make; we go a
# bit larger to give the model useful gradient.
DEFAULT_SEARCH_RADIUS_M = 1_000.0


@dataclass
class NhdFlowline:
    gnis_name: str
    fcode: int
    coords: list[list[float]]  # [[lon, lat], ...]


@dataclass
class NavWatersResult:
    inside: list[NhdFlowline] = field(default_factory=list)
    nearest_outside: NhdFlowline | None = None
    nearest_distance_m: float | None = None
    search_radius_m: float = DEFAULT_SEARCH_RADIUS_M

    @property
    def has_permanent_water_in_aoi(self) -> bool:
        return any(f.fcode in FCODES_PERMANENT for f in self.inside)


def query_navigable_waters(
    aoi: AOI,
    *,
    search_radius_m: float = DEFAULT_SEARCH_RADIUS_M,
) -> NavWatersResult:
    """Find perennial flowlines inside the AOI, and the nearest
    permanent flowline within ``search_radius_m`` if none intersect."""

    inside = _query_flowlines(aoi.arcgis_geometry(), permanent_only=True)

    result = NavWatersResult(inside=inside, search_radius_m=search_radius_m)
    if inside:
        return result

    # No permanent water inside the AOI — look outward.
    buffered = _buffer_geometry(aoi, search_radius_m)
    candidates = _query_flowlines(buffered, permanent_only=True)

    lon, lat = aoi.centroid()
    best: tuple[float, NhdFlowline] | None = None
    for f in candidates:
        d = min_distance_to_line_m(lon, lat, f.coords)
        if best is None or d < best[0]:
            best = (d, f)

    if best is not None:
        result.nearest_distance_m = best[0]
        result.nearest_outside = best[1]

    log.info(
        "NHD: %d flowlines in AOI; nearest outside = %s m",
        len(inside),
        f"{result.nearest_distance_m:.0f}" if result.nearest_distance_m else "n/a",
    )
    return result


def _query_flowlines(
    esri_geom: dict, *, permanent_only: bool
) -> list[NhdFlowline]:
    where = "1=1"
    if permanent_only:
        codes = ",".join(str(c) for c in FCODES_PERMANENT)
        where = f"FCODE IN ({codes})"

    params = {
        "f": "json",
        "geometry": json.dumps(esri_geom),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": where,
        "outFields": "GNIS_NAME,FCODE",
        "returnGeometry": "true",
        "outSR": 4326,
    }
    payload = get_json(NHD_FLOWLINE_URL, params=params)

    flowlines: list[NhdFlowline] = []
    for feat in payload.get("features", []):
        attrs = feat.get("attributes", {})
        paths = (feat.get("geometry") or {}).get("paths") or []
        # A flowline can have multiple paths — flatten
        coords = [pt for path in paths for pt in path]
        flowlines.append(
            NhdFlowline(
                gnis_name=(attrs.get("GNIS_NAME") or "").strip(),
                fcode=int(attrs.get("FCODE") or 0),
                coords=coords,
            )
        )
    return flowlines


def _buffer_geometry(aoi: AOI, radius_m: float) -> dict:
    """Rough WGS84 buffer of the AOI bounding box by ``radius_m``.

    Good enough as a spatial pre-filter — we compute exact distances
    against the returned flowlines afterwards."""
    minx, miny, maxx, maxy = aoi.bbox()
    lat_mid = (miny + maxy) / 2.0
    dlat = radius_m / 111_132.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat_mid)), 1e-6))
    ring = [
        [minx - dlon, miny - dlat],
        [maxx + dlon, miny - dlat],
        [maxx + dlon, maxy + dlat],
        [minx - dlon, maxy + dlat],
        [minx - dlon, miny - dlat],
    ]
    return {"rings": [ring], "spatialReference": {"wkid": 4326}}
