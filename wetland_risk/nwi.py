"""National Wetlands Inventory (USFWS) client.

Uses the public ArcGIS MapServer at:

    https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0

Layer 0 is the Wetlands polygon feature class. The important
attributes for jurisdictional analysis are:

    ATTRIBUTE   — Cowardin classification code (e.g. "PEM1Ah", "R2UBH")
    WETLAND_TYPE— Human-readable type ("Freshwater Emergent Wetland", ...)
    ACRES       — Polygon area

Cowardin system codes:
    P — Palustrine  (most common inland wetlands)
    L — Lacustrine  (lakes)
    R — Riverine    (rivers/streams)
    E — Estuarine   (brackish/tidal)
    M — Marine
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ._http import get_json
from .aoi import AOI

log = logging.getLogger(__name__)

NWI_QUERY_URL = (
    "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice"
    "/rest/services/Wetlands/MapServer/0/query"
)


@dataclass
class NwiPolygon:
    cowardin: str          # ATTRIBUTE
    wetland_type: str      # WETLAND_TYPE
    acres: float
    system: str            # 'P', 'L', 'R', 'E', 'M', or '?'

    @property
    def is_tidal(self) -> bool:
        return self.system in ("E", "M")

    @property
    def is_riverine_or_lacustrine(self) -> bool:
        return self.system in ("R", "L")


@dataclass
class NwiResult:
    polygons: list[NwiPolygon] = field(default_factory=list)

    @property
    def any_present(self) -> bool:
        return bool(self.polygons)

    @property
    def total_acres(self) -> float:
        return sum(p.acres for p in self.polygons)

    def dominant_system(self) -> str | None:
        """System letter with the most acreage inside the AOI."""
        if not self.polygons:
            return None
        by_sys: dict[str, float] = {}
        for p in self.polygons:
            by_sys[p.system] = by_sys.get(p.system, 0.0) + p.acres
        return max(by_sys, key=by_sys.get)


def query_nwi(aoi: AOI) -> NwiResult:
    """Return every NWI wetland polygon that intersects the AOI."""
    params = {
        "f": "json",
        "geometry": json.dumps(aoi.arcgis_geometry()),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ATTRIBUTE,WETLAND_TYPE,ACRES",
        "returnGeometry": "false",
        "outSR": 4326,
    }
    payload = get_json(NWI_QUERY_URL, params=params)

    polygons: list[NwiPolygon] = []
    for feat in payload.get("features", []):
        attrs = feat.get("attributes", {})
        code = (attrs.get("ATTRIBUTE") or "").strip()
        polygons.append(
            NwiPolygon(
                cowardin=code,
                wetland_type=(attrs.get("WETLAND_TYPE") or "").strip(),
                acres=float(attrs.get("ACRES") or 0.0),
                system=code[:1].upper() if code else "?",
            )
        )
    log.info("NWI: %d polygons intersecting AOI", len(polygons))
    return NwiResult(polygons=polygons)
