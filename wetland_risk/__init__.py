"""Jurisdictional wetland risk assessment.

Combines three federal datasets to score the likelihood that an
area of interest contains wetlands regulated under Clean Water Act
Section 404:

  * NWI  — USFWS National Wetlands Inventory (wetland polygons + codes)
  * SSURGO — USDA/NRCS soil survey (hydric soil rating)
  * NHD  — USGS National Hydrography Dataset (perennial flowlines,
           used as a proxy for "relatively permanent waters" / TNW
           connectivity per Sackett v. EPA, 598 U.S. 651 (2023))

The output is a *risk score*, not a jurisdictional determination.
Only the U.S. Army Corps of Engineers issues JDs.
"""

from .aoi import AOI
from .risk import RiskReport, assess

__all__ = ["AOI", "RiskReport", "assess"]
