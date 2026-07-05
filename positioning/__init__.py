"""Dealer/participant positioning — Phase A of the dealer-gamma program.

Modules:
  participant.py — thin re-export of the NSE participant-OI parse layer
                   (fii_dii_scraper.py remains the fetch implementation)
  anchor.py      — daily dealer-sign scores derived from participant OI,
                   plus the persistent history table (data/participant_history.csv)
  backfill.py    — idempotent multi-year NSE archive walk to build the table
"""
