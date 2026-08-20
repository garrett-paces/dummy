"""Offline tests for the scoring layer.

We patch the three service clients so the tests never hit the network.
The goal is to lock the sub-score curves and the banding logic, not
to re-verify the ArcGIS endpoints (that's what a live smoke test is
for — see README).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from wetland_risk import assess, AOI
from wetland_risk.navwaters import NavWatersResult, NhdFlowline
from wetland_risk.nwi import NwiPolygon, NwiResult
from wetland_risk.risk import (
    _band,
    _connectivity_score,
    _hydric_score,
    _wetland_score,
)
from wetland_risk.soils import MapUnit, SoilsResult


def _aoi():
    return AOI.from_bbox(-95.65, 38.93, -95.60, 38.97)


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------

def test_connectivity_perennial_in_aoi_is_max():
    nav = NavWatersResult(
        inside=[NhdFlowline("Kaw River", 46006, [[0, 0], [1, 1]])],
    )
    assert _connectivity_score(nav) == 1.0


def test_connectivity_adjacent_is_max():
    nav = NavWatersResult(
        inside=[],
        nearest_outside=NhdFlowline("x", 46006, [[0, 0], [1, 1]]),
        nearest_distance_m=15.0,
    )
    assert _connectivity_score(nav) == 1.0


def test_connectivity_far_is_zero():
    nav = NavWatersResult(
        inside=[],
        nearest_outside=NhdFlowline("x", 46006, [[0, 0], [1, 1]]),
        nearest_distance_m=1_000.0,
        search_radius_m=1_000.0,
    )
    assert _connectivity_score(nav) == 0.0


def test_connectivity_falloff_is_monotonic():
    prev = 1.01
    for d in (30, 60, 120, 150, 300, 600, 999):
        nav = NavWatersResult(
            inside=[],
            nearest_outside=NhdFlowline("x", 46006, [[0, 0]]),
            nearest_distance_m=float(d),
            search_radius_m=1_000.0,
        )
        s = _connectivity_score(nav)
        assert 0.0 <= s <= 1.0
        assert s <= prev + 1e-9, f"non-monotonic at d={d}: {s} > {prev}"
        prev = s


def test_wetland_score_no_polygons():
    assert _wetland_score(NwiResult(polygons=[])) == 0.0


def test_wetland_score_estuarine_max():
    r = NwiResult(polygons=[NwiPolygon("E2EM1P", "Estuarine and Marine Wetland", 5.0, "E")])
    assert _wetland_score(r) == 1.0


def test_wetland_score_palustrine_lower_than_riverine():
    p = NwiResult(polygons=[NwiPolygon("PEM1A", "Freshwater Emergent Wetland", 2.0, "P")])
    r = NwiResult(polygons=[NwiPolygon("R2UBH", "Riverine", 2.0, "R")])
    assert _wetland_score(p) < _wetland_score(r)


def test_hydric_score_matches_fraction():
    soils = SoilsResult(map_units=[
        MapUnit("1", "A", "Yes", 5.0),
        MapUnit("2", "B", "No", 5.0),
    ])
    # 5 acres * 1.0 + 5 * 0 → fraction 0.5
    assert _hydric_score(soils) == pytest.approx(0.5)


def test_hydric_partial_counts_half():
    soils = SoilsResult(map_units=[
        MapUnit("1", "A", "Partially Hydric", 10.0),
    ])
    assert _hydric_score(soils) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Banding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,band", [
    (0.05, "MINIMAL"),
    (0.20, "LOW"),
    (0.50, "MODERATE"),
    (0.80, "HIGH"),
])
def test_bands(score, band):
    assert _band(score) == band


# ---------------------------------------------------------------------------
# End-to-end (mocked service calls)
# ---------------------------------------------------------------------------

def test_assess_high_when_everything_lights_up():
    nwi = NwiResult(polygons=[
        NwiPolygon("PEM1Ah", "Freshwater Emergent Wetland", 3.5, "P"),
    ])
    soils = SoilsResult(map_units=[
        MapUnit("1", "Hydric silty clay", "Yes", 4.0),
    ])
    nav = NavWatersResult(
        inside=[NhdFlowline("Wakarusa River", 46006, [[-95.62, 38.95]])],
    )

    with patch("wetland_risk.risk.query_nwi", return_value=nwi), \
         patch("wetland_risk.risk.query_hydric_soils", return_value=soils), \
         patch("wetland_risk.risk.query_navigable_waters", return_value=nav):
        report = assess(_aoi())

    assert report.band == "HIGH"
    assert report.overall_score >= 0.70
    assert any("perennial" in line.lower() for line in report.rationale)


def test_assess_minimal_when_nothing_present():
    nwi = NwiResult(polygons=[])
    soils = SoilsResult(map_units=[MapUnit("1", "Upland loam", "No", 40.0)])
    nav = NavWatersResult(inside=[], nearest_distance_m=None)

    with patch("wetland_risk.risk.query_nwi", return_value=nwi), \
         patch("wetland_risk.risk.query_hydric_soils", return_value=soils), \
         patch("wetland_risk.risk.query_navigable_waters", return_value=nav):
        report = assess(_aoi())

    assert report.band == "MINIMAL"
    assert report.overall_score < 0.15


def test_assess_moderate_when_wetland_but_no_connectivity():
    nwi = NwiResult(polygons=[
        NwiPolygon("PEM1A", "Freshwater Emergent Wetland", 1.0, "P"),
    ])
    soils = SoilsResult(map_units=[MapUnit("1", "Hydric silt", "Yes", 1.0)])
    nav = NavWatersResult(inside=[], nearest_distance_m=None,
                          search_radius_m=1_000.0)

    with patch("wetland_risk.risk.query_nwi", return_value=nwi), \
         patch("wetland_risk.risk.query_hydric_soils", return_value=soils), \
         patch("wetland_risk.risk.query_navigable_waters", return_value=nav):
        report = assess(_aoi())

    # Wetland + hydric present but no connectivity → moderate/low, not
    # high; Sackett gives connectivity most of the weight.
    assert report.band in ("MODERATE", "LOW")
    assert report.scores["connectivity"] == 0.0
