"""USDA/NRCS hydric-soils client via Soil Data Access (SDA).

SDA is a public REST endpoint that accepts SQL queries against the
SSURGO tabular schema and a spatial component. See:

    https://sdmdataaccess.nrcs.usda.gov/

We use the spatial helper ``SDA_Get_Mukey_from_intersection_with_WktWgs84``
(exposed as ``SDA_Get_MupolygonWktWgs84_from_Mukey`` etc.) to find map
unit keys (MUKEYs) that intersect the AOI, then join to the
``mapunit`` table for the hydric-classification rating.

Hydric ratings we care about (mapunit.hydricrating):

    "Yes"                — all components hydric
    "Partially Hydric"   — some components hydric
    "Unranked"           — no rating
    "No"                 — no hydric components
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ._http import post_json
from .aoi import AOI

log = logging.getLogger(__name__)

SDA_URL = "https://sdmdataaccess.nrcs.usda.gov/Tabular/SDMTabularService/post.rest"

# Weight of "Yes" and "Partially" in the composite hydric fraction
_HYDRIC_WEIGHT = {"Yes": 1.0, "Partially Hydric": 0.5, "Unranked": 0.0, "No": 0.0}


@dataclass
class MapUnit:
    mukey: str
    muname: str
    hydric_rating: str  # 'Yes' | 'Partially Hydric' | 'Unranked' | 'No'
    area_acres: float   # intersected acres inside the AOI


@dataclass
class SoilsResult:
    map_units: list[MapUnit] = field(default_factory=list)

    @property
    def total_acres(self) -> float:
        return sum(m.area_acres for m in self.map_units)

    @property
    def hydric_fraction(self) -> float:
        """Acre-weighted fraction of the AOI that is hydric.

        "Partially Hydric" counts at 0.5. Returns 0 when SDA returned
        nothing (usually means the AOI is offshore or in Alaska where
        SSURGO coverage is sparse — the caller should treat that as
        low-confidence rather than "not hydric")."""
        total = self.total_acres
        if total <= 0:
            return 0.0
        return sum(
            _HYDRIC_WEIGHT.get(m.hydric_rating, 0.0) * m.area_acres
            for m in self.map_units
        ) / total


def query_hydric_soils(aoi: AOI) -> SoilsResult:
    """Return hydric-soil map units intersecting the AOI.

    Uses SDA's WKT-based spatial helper. The AOI is converted to a
    POLYGON WKT string in WGS84.
    """
    wkt = _aoi_to_wkt(aoi)
    query = f"""
        SELECT
            mu.mukey,
            mu.muname,
            ISNULL(mu.hydricrating, 'Unranked') AS hydricrating,
            geom.STIntersection(
                geometry::STGeomFromText('{wkt}', 4326)
            ).STArea() AS shared_area_sq_deg
        FROM SDA_Get_MupolygonWktWgs84_from_Mukey(
            (SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84(
                '{wkt}'
            ))
        ) AS s
        INNER JOIN mapunit AS mu ON s.mukey = mu.mukey
    """.strip()

    # SDA accepts either raw SQL via 'query' or a JSON envelope; the
    # POST 'FORMAT: JSON' body is the most robust option.
    payload = post_json(
        SDA_URL,
        data={"query": query, "format": "JSON"},
    )

    rows = payload.get("Table", []) or []
    map_units: list[MapUnit] = []
    for row in rows:
        mukey, muname, hydric, sq_deg = row
        map_units.append(
            MapUnit(
                mukey=str(mukey),
                muname=str(muname),
                hydric_rating=str(hydric),
                area_acres=_sq_deg_to_acres(float(sq_deg or 0.0), aoi),
            )
        )
    log.info("SSURGO: %d hydric-classified map units in AOI", len(map_units))
    return SoilsResult(map_units=map_units)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _aoi_to_wkt(aoi: AOI) -> str:
    g = aoi.geometry
    if g["type"] == "Polygon":
        return _polygon_wkt(g["coordinates"])
    # MultiPolygon
    parts = [f"({_polygon_wkt(poly)[8:]})" for poly in g["coordinates"]]
    return f"MULTIPOLYGON({', '.join(parts)})"


def _polygon_wkt(rings: list[list[list[float]]]) -> str:
    ring_strs = []
    for ring in rings:
        pts = ", ".join(f"{x} {y}" for x, y in ring)
        ring_strs.append(f"({pts})")
    return f"POLYGON({', '.join(ring_strs)})"


def _sq_deg_to_acres(sq_deg: float, aoi: AOI) -> float:
    """SQL Server's STArea() on a geometry (not geography) returns
    the area in whatever units the coordinates use — for WGS84 that's
    square degrees. Convert using a local scale factor at the AOI's
    centroid latitude."""
    import math
    _, lat = aoi.centroid()
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    sq_m = sq_deg * m_per_deg_lat * m_per_deg_lon
    return sq_m / 4046.8564224
