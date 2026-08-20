"""Command-line entry point.

    python -m wetland_risk --bbox -95.65 38.93 -95.60 38.97
    python -m wetland_risk --geojson parcel.geojson --out report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .aoi import AOI
from .risk import assess

log = logging.getLogger("wetland_risk")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wetland_risk",
        description=(
            "Screen an area of interest for CWA §404 jurisdictional "
            "wetland risk using NWI, SSURGO, and NHD."
        ),
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MINX", "MINY", "MAXX", "MAXY"),
        help="AOI bounding box in WGS84 lon/lat.",
    )
    src.add_argument(
        "--geojson",
        help="Path to a GeoJSON Feature/FeatureCollection/Geometry file.",
    )
    parser.add_argument(
        "--search-radius",
        type=float,
        default=1_000.0,
        help="Meters to search outward for the nearest perennial "
             "flowline when none is inside the AOI (default: 1000).",
    )
    parser.add_argument(
        "--out",
        help="Write full JSON report to this path. If omitted, prints "
             "a short summary to stdout.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.bbox:
        aoi = AOI.from_bbox(*args.bbox)
    else:
        aoi = AOI.from_geojson(args.geojson)

    report = assess(aoi, search_radius_m=args.search_radius)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report.to_dict(), f, indent=2, default=str)
        log.info("Wrote %s", args.out)

    _print_summary(report)
    return 0


def _print_summary(report) -> None:
    print()
    print(f"  Overall risk : {report.band}  ({report.overall_score:.2f})")
    for k, v in report.scores.items():
        print(f"    - {k:<13}: {v:.2f}")
    print()
    for line in report.rationale:
        print(f"  • {line}")
    print()
    print("  Disclaimer: screening only; not an Approved JD.")
    print()


if __name__ == "__main__":
    sys.exit(main())
