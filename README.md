# dummy

Two unrelated tools live in this repo:

1. `wetland_risk/` — jurisdictional wetland risk screener (see below)
2. `scraper.py` — De Soto, KS city-code → PDF scraper (see [SCRAPER.md](#de-soto-kansas-city-code-scraper))

---

## Wetland jurisdictional risk

`wetland_risk` is a screening tool that combines three federal
datasets to estimate the risk that an area of interest (AOI)
contains wetlands regulated under Clean Water Act §404:

| Dataset | Source | Signal |
|---|---|---|
| **NWI** — National Wetlands Inventory | USFWS ArcGIS MapServer | Mapped wetland polygons + Cowardin type |
| **Hydric soils** — SSURGO | USDA/NRCS Soil Data Access | Acre-weighted hydric-soil fraction |
| **Navigable / permanent waters** — NHDPlus HR | USGS `hydro.nationalmap.gov` | Perennial flowlines in/near the AOI |

The scoring model is tuned to the post-*Sackett v. EPA* (2023)
"continuous surface connection" test. Connectivity to a relatively
permanent water gets the most weight (0.50), NWI presence next
(0.30), hydric soils last (0.20). Weights are exposed in
`wetland_risk.risk.RISK_WEIGHTS` — override them for your workflow.

**This is a screening tool, not an Approved Jurisdictional
Determination.** Only the U.S. Army Corps of Engineers can issue a JD.

### Install

```bash
pip install requests
```

### Use

```bash
# Bounding box (WGS84 lon/lat)
python -m wetland_risk --bbox -95.65 38.93 -95.60 38.97

# Parcel polygon from GeoJSON
python -m wetland_risk --geojson parcel.geojson --out report.json

# Widen the search for a nearby perennial flowline (default 1000 m)
python -m wetland_risk --geojson parcel.geojson --search-radius 2000
```

Programmatically:

```python
from wetland_risk import AOI, assess

aoi = AOI.from_bbox(-95.65, 38.93, -95.60, 38.97)
report = assess(aoi)

print(report.band, report.overall_score)
for line in report.rationale:
    print("•", line)
```

`RiskReport.to_dict()` returns a JSON-serializable dict with the raw
polygons, map units, and flowlines used for the score — useful for
feeding a downstream GIS review.

### Output bands

| Band | Score | Meaning |
|---|---|---|
| HIGH | ≥ 0.70 | Likely jurisdictional; request a JD or plan around it |
| MODERATE | ≥ 0.40 | Ambiguous; site visit warranted |
| LOW | ≥ 0.15 | Unlikely but not zero |
| MINIMAL | < 0.15 | No wetland indicators found |

### Tests

```bash
python -m pytest tests/ -q
```

Tests mock the three service clients — they lock the scoring math and
banding, not the remote services.

### Known limits

- Coverage gaps: SSURGO is sparse in parts of Alaska and offshore.
  A zero hydric fraction there means "unknown", not "not hydric".
- The "navigable waters" signal is a proxy: we approximate RPW/TNW
  with NHDPlus HR perennial flowlines. The Corps' Navigable Waters
  List is more restrictive; this tool errs toward flagging risk.
- Distances are computed to the AOI centroid, not the AOI boundary —
  fine for small parcels, coarse for large tracts. If you're screening
  a large tract, tile it and assess each tile.

---

## De Soto Kansas City Code Scraper

Scrapes all pages from the [Code of the City of De Soto, Kansas](https://desotokansas.citycode.net/index.html#!codeOfTheCityOfDeSotoKansas) and consolidates them into a single PDF.

### Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

### Usage

```bash
python3 scraper.py                       # writes desoto_city_code.pdf
python3 scraper.py -o my_output.pdf
python3 scraper.py --keep-temp
python3 scraper.py --concurrency 1
python3 scraper.py -v
```
