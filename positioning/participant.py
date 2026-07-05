"""Participant-OI parse layer — re-exported from fii_dii_scraper.

The scraper owns the NSE fetch quirks (cookie prefetch, dual archive hosts,
browser headers) and the CSV column map; this module is the package-stable
import point so engine code never imports the root-level script directly.
"""
from __future__ import annotations

from fii_dii_scraper import (  # noqa: F401
    FIISnapshot,
    ParticipantOI,
    NSE_HEADERS,
    _fetch_csv,
    _parse_csv,
    fetch_fii_snapshot,
    fetch_latest_fii_data,
)
