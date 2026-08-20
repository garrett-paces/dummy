"""Area-of-interest primitives.

An ``AOI`` is either a bounding box or a GeoJSON polygon in WGS84
(EPSG:4326). Everything downstream uses it to form ArcGIS REST
`geometry` parameters and to compute distances.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AOI:
    """Area of interest in WGS84.

    Use ``AOI.from_bbox`` for a quick rectangle or ``AOI.from_geojson``
    for a real parcel boundary. ``geometry`` is always stored as
    GeoJSON so the same object works for both spatial queries and
    reporting.
    """

    geometry: dict[str, Any]  # GeoJSON geometry (Polygon or MultiPolygon)

    @classmethod
    def from_bbox(cls, minx: float, miny: float, maxx: float, maxy: float) -> "AOI":
        ring = [
            [minx, miny],
            [maxx, miny],
            [maxx, maxy],
            [minx, maxy],
            [minx, miny],
        ]
        return cls({"type": "Polygon", "coordinates": [ring]})

    @classmethod
    def from_geojson(cls, path_or_dict: str | Path | dict) -> "AOI":
        if isinstance(path_or_dict, (str, Path)):
            data = json.loads(Path(path_or_dict).read_text())
        else:
            data = path_or_dict

        # Accept a Feature, FeatureCollection (first feature), or bare geometry
        if data.get("type") == "FeatureCollection":
            data = data["features"][0]
        if data.get("type") == "Feature":
            data = data["geometry"]
        if data["type"] not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"AOI must be Polygon/MultiPolygon, got {data['type']}")
        return cls(data)

    # ---- Derived views ------------------------------------------------

    def bbox(self) -> tuple[float, float, float, float]:
        """Return (minx, miny, maxx, maxy) in WGS84."""
        xs, ys = [], []
        for x, y in _iter_coords(self.geometry):
            xs.append(x)
            ys.append(y)
        return min(xs), min(ys), max(xs), max(ys)

    def centroid(self) -> tuple[float, float]:
        """Cheap centroid — average of vertices. Good enough for
        pick-a-state and distance-from-AOI-center use cases."""
        xs, ys = zip(*_iter_coords(self.geometry))
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def arcgis_geometry(self) -> dict[str, Any]:
        """Convert to Esri JSON geometry shape used by ArcGIS REST
        `geometry` query parameters."""
        if self.geometry["type"] == "Polygon":
            rings = self.geometry["coordinates"]
        else:  # MultiPolygon
            rings = [ring for poly in self.geometry["coordinates"] for ring in poly]
        return {"rings": rings, "spatialReference": {"wkid": 4326}}


def _iter_coords(geom: dict[str, Any]):
    if geom["type"] == "Polygon":
        for ring in geom["coordinates"]:
            yield from ring
    elif geom["type"] == "MultiPolygon":
        for poly in geom["coordinates"]:
            for ring in poly:
                yield from ring
    else:
        raise ValueError(f"Unsupported geometry: {geom['type']}")


# ---------------------------------------------------------------------------
# Geodesic helpers
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in meters. Accurate enough (<0.5%) at the
    scales we care about (< a few km from AOI to a flowline)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def min_distance_to_line_m(
    aoi_lon: float, aoi_lat: float, line_coords: list[list[float]]
) -> float:
    """Minimum distance in meters from a point to a polyline whose
    coordinates are [lon, lat] pairs. Uses per-segment nearest-point
    projection in a local equirectangular approximation — fine for the
    sub-kilometer distances that matter for adjacency."""
    if not line_coords:
        return float("inf")

    best = float("inf")
    for i in range(len(line_coords) - 1):
        d = _point_to_segment_m(aoi_lon, aoi_lat, line_coords[i], line_coords[i + 1])
        if d < best:
            best = d
    # Also consider distance to the last vertex in case the line is a single point
    if len(line_coords) == 1:
        lo, la = line_coords[0]
        best = haversine_m(aoi_lon, aoi_lat, lo, la)
    return best


def _point_to_segment_m(
    plon: float, plat: float, a: list[float], b: list[float]
) -> float:
    """Distance from point to segment using a local ENU-ish
    approximation centered on the point."""
    lat0 = math.radians(plat)
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(lat0)

    px, py = 0.0, 0.0
    ax = (a[0] - plon) * m_per_deg_lon
    ay = (a[1] - plat) * m_per_deg_lat
    bx = (b[0] - plon) * m_per_deg_lon
    by = (b[1] - plat) * m_per_deg_lat

    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.hypot(ax - px, ay - py)

    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(cx - px, cy - py)
