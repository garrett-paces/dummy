"""Composite jurisdictional-risk scoring.

Scoring philosophy
------------------

We produce three sub-scores in [0, 1] and combine them into an overall
score with fixed weights. The weights encode the post-*Sackett* legal
landscape: a wetland is a Water of the United States if it has a
"continuous surface connection" to a relatively permanent water,
such that the wetland is indistinguishable from the water itself.

    connectivity  (0.50)  — proximity to permanent NHD flowlines
    wetland       (0.30)  — NWI presence + tidal/riverine bias
    hydric_soil   (0.20)  — acre-weighted hydric fraction

The weights are exposed as ``RISK_WEIGHTS`` so callers can override.

The output banding is coarse on purpose:

    >= 0.70 → HIGH        likely jurisdictional; plan a JD request
    >= 0.40 → MODERATE    ambiguous; site visit warranted
    >= 0.15 → LOW         unlikely but not zero
    <  0.15 → MINIMAL     no wetland indicators found

This is a *screening* tool. Final jurisdictional determinations come
from the Corps District office.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .aoi import AOI
from .navwaters import NavWatersResult, query_navigable_waters
from .nwi import NwiResult, query_nwi
from .soils import SoilsResult, query_hydric_soils

log = logging.getLogger(__name__)

RISK_WEIGHTS = {
    "connectivity": 0.50,
    "wetland": 0.30,
    "hydric_soil": 0.20,
}

# Distance below which a wetland is treated as effectively adjacent
# (score 1.0). Above the search radius the score is 0.
_ADJACENT_M = 30.0        # ~100 ft
_CONNECTIVITY_FLOOR = 150.0  # meters; roughly the "close enough to argue" line


@dataclass
class RiskReport:
    aoi: dict[str, Any]
    nwi: NwiResult
    soils: SoilsResult
    nav_waters: NavWatersResult

    scores: dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    band: str = "MINIMAL"
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "aoi": self.aoi,
            "nwi": {
                "polygon_count": len(self.nwi.polygons),
                "total_acres": round(self.nwi.total_acres, 3),
                "dominant_system": self.nwi.dominant_system(),
                "polygons": [asdict(p) for p in self.nwi.polygons],
            },
            "soils": {
                "map_unit_count": len(self.soils.map_units),
                "hydric_fraction": round(self.soils.hydric_fraction, 3),
                "map_units": [asdict(m) for m in self.soils.map_units],
            },
            "nav_waters": {
                "flowlines_in_aoi": len(self.nav_waters.inside),
                "has_permanent_water_in_aoi": self.nav_waters.has_permanent_water_in_aoi,
                "nearest_distance_m": self.nav_waters.nearest_distance_m,
                "nearest_name": (
                    self.nav_waters.nearest_outside.gnis_name
                    if self.nav_waters.nearest_outside
                    else None
                ),
                "search_radius_m": self.nav_waters.search_radius_m,
            },
            "scores": self.scores,
            "overall_score": self.overall_score,
            "band": self.band,
            "rationale": self.rationale,
            "disclaimer": (
                "This is a screening-level risk score, not an "
                "Approved Jurisdictional Determination. Only the "
                "U.S. Army Corps of Engineers can issue a JD."
            ),
        }


def assess(
    aoi: AOI,
    *,
    weights: dict[str, float] | None = None,
    search_radius_m: float = 1_000.0,
) -> RiskReport:
    """Run all three queries and produce a scored ``RiskReport``.

    Any single data source failing raises — the caller decides how to
    handle offline services, since a partial report can be misleading
    (e.g. reporting "low risk" purely because NWI is unreachable).
    """
    nwi = query_nwi(aoi)
    soils = query_hydric_soils(aoi)
    nav = query_navigable_waters(aoi, search_radius_m=search_radius_m)

    scores = {
        "connectivity": _connectivity_score(nav),
        "wetland": _wetland_score(nwi),
        "hydric_soil": _hydric_score(soils),
    }
    w = weights or RISK_WEIGHTS
    overall = sum(scores[k] * w[k] for k in scores)

    rationale = _explain(nwi, soils, nav, scores)
    band = _band(overall)

    return RiskReport(
        aoi=aoi.geometry,
        nwi=nwi,
        soils=soils,
        nav_waters=nav,
        scores=scores,
        overall_score=round(overall, 3),
        band=band,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------

def _connectivity_score(nav: NavWatersResult) -> float:
    if nav.has_permanent_water_in_aoi:
        return 1.0
    d = nav.nearest_distance_m
    if d is None:
        return 0.0
    if d <= _ADJACENT_M:
        return 1.0
    if d >= nav.search_radius_m:
        return 0.0
    # Linear falloff between _ADJACENT_M and search_radius_m, with a
    # kink at _CONNECTIVITY_FLOOR so "close but not touching" still
    # scores meaningfully.
    if d <= _CONNECTIVITY_FLOOR:
        # 30 m → 1.0 ; 150 m → 0.6
        return 1.0 - 0.4 * (d - _ADJACENT_M) / (_CONNECTIVITY_FLOOR - _ADJACENT_M)
    # 150 m → 0.6 ; search_radius → 0
    return 0.6 * (1 - (d - _CONNECTIVITY_FLOOR) / (nav.search_radius_m - _CONNECTIVITY_FLOOR))


def _wetland_score(nwi: NwiResult) -> float:
    if not nwi.any_present:
        return 0.0
    # Base credit for any mapped wetland
    score = 0.6
    dom = nwi.dominant_system()
    if dom in ("E", "M"):        # tidal — very likely jurisdictional
        score = 1.0
    elif dom in ("R", "L"):      # riverine/lacustrine — usually jurisdictional
        score = 0.9
    elif dom == "P":             # palustrine — depends on connectivity
        score = 0.7
    # Small acreage bonus (capped): a 20-acre wetland reads bigger than 0.1 acres
    if nwi.total_acres >= 1.0:
        score = min(1.0, score + 0.05)
    if nwi.total_acres >= 10.0:
        score = min(1.0, score + 0.05)
    return score


def _hydric_score(soils: SoilsResult) -> float:
    # Acre-weighted hydric fraction ∈ [0, 1] already.
    return max(0.0, min(1.0, soils.hydric_fraction))


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def _band(overall: float) -> str:
    if overall >= 0.70:
        return "HIGH"
    if overall >= 0.40:
        return "MODERATE"
    if overall >= 0.15:
        return "LOW"
    return "MINIMAL"


def _explain(
    nwi: NwiResult,
    soils: SoilsResult,
    nav: NavWatersResult,
    scores: dict[str, float],
) -> list[str]:
    lines: list[str] = []

    if nwi.any_present:
        dom = nwi.dominant_system()
        lines.append(
            f"NWI shows {len(nwi.polygons)} wetland polygon(s) "
            f"({nwi.total_acres:.2f} ac) intersecting the AOI; dominant "
            f"system '{dom}'."
        )
    else:
        lines.append("No mapped NWI wetlands intersect the AOI.")

    hf = soils.hydric_fraction
    if hf >= 0.5:
        lines.append(f"Soils are predominantly hydric (fraction={hf:.2f}).")
    elif hf > 0:
        lines.append(f"AOI contains some hydric-rated soils (fraction={hf:.2f}).")
    else:
        lines.append("No hydric soil rating detected in the AOI.")

    if nav.has_permanent_water_in_aoi:
        lines.append(
            "A perennial NHD flowline runs through the AOI — likely "
            "a relatively permanent water (RPW)."
        )
    elif nav.nearest_distance_m is not None:
        name = nav.nearest_outside.gnis_name or "unnamed perennial flowline"
        lines.append(
            f"Nearest permanent NHD flowline ({name}) is "
            f"{nav.nearest_distance_m:.0f} m from the AOI."
        )
    else:
        lines.append(
            f"No perennial NHD flowline within {nav.search_radius_m:.0f} m "
            "of the AOI."
        )

    lines.append(
        f"Sub-scores: connectivity={scores['connectivity']:.2f}, "
        f"wetland={scores['wetland']:.2f}, "
        f"hydric_soil={scores['hydric_soil']:.2f}."
    )
    return lines
