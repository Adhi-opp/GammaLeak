"""Idempotent multi-year backfill of data/participant_history.csv from the
NSE archives.

Walks weekdays from --from (default: 3 years back) to the last completed
trading day, skipping dates already present in the history table, fetching
fao_participant_oi_DDMMYYYY.csv via the scraper's session pattern (cookie
prefetch + browser headers + dual archive hosts) and upserting the parsed
rows. Holidays 404 and are simply skipped; parse failures are logged and
skipped (a format change on old files must never abort the walk).

Polite pacing: PACING_SECS between requests (~780 trading days over 3 years
≈ 8-10 minutes). Progress is flushed to the history file every FLUSH_EVERY
successful rows, so an interrupted run resumes where it stopped.

Usage:
    python -m positioning.backfill                     # 3 years
    python -m positioning.backfill --from 2025-07-01
    python -m positioning.backfill --dry-run           # list missing dates only
"""
from __future__ import annotations

import argparse
import asyncio
import ssl
from datetime import date, datetime, timedelta, timezone

import aiohttp

from fii_dii_scraper import NSE_HEADERS, _fetch_csv, _parse_csv
from positioning.anchor import (
    HISTORY_PATH,
    load_history,
    snapshot_to_row,
    write_history,
)

IST = timezone(timedelta(hours=5, minutes=30))
PACING_SECS = 0.35
FLUSH_EVERY = 25


def _last_completed_trading_day() -> date:
    d = datetime.now(IST)
    day = d.date() if d.hour >= 20 else d.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def missing_weekdays(from_day: date, to_day: date, have: set[str]) -> list[date]:
    out = []
    d = from_day
    while d <= to_day:
        if d.weekday() < 5 and d.isoformat() not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


async def run_backfill(from_day: date, to_day: date, dry_run: bool = False) -> None:
    rows = load_history(HISTORY_PATH)
    todo = missing_weekdays(from_day, to_day, set(rows))
    print(f"history has {len(rows)} rows; {len(todo)} candidate weekdays to fetch "
          f"({from_day} .. {to_day})")
    if dry_run or not todo:
        return

    ssl_ctx = ssl.create_default_context()
    conn = aiohttp.TCPConnector(ssl=ssl_ctx)
    fetched = skipped = failed = 0
    async with aiohttp.ClientSession(headers=NSE_HEADERS, connector=conn) as session:
        # NSE wants a cookie before serving archive CSVs
        try:
            async with session.get("https://www.nseindia.com/",
                                   timeout=aiohttp.ClientTimeout(total=10)):
                pass
        except Exception:
            pass

        for i, d in enumerate(todo):
            try:
                text = await _fetch_csv(session, d)
                snap = _parse_csv(text, d)
                rows[d.isoformat()] = snapshot_to_row(snap)
                fetched += 1
            except FileNotFoundError:
                skipped += 1          # holiday / not published — normal
            except Exception as exc:  # format drift on old files etc.
                failed += 1
                print(f"  [{d}] parse/fetch problem (skipped): {exc}")
            if fetched and fetched % FLUSH_EVERY == 0:
                write_history(rows, HISTORY_PATH)
            if (i + 1) % 100 == 0:
                print(f"  progress {i+1}/{len(todo)}  (+{fetched} rows, "
                      f"{skipped} holidays, {failed} failures)")
            await asyncio.sleep(PACING_SECS)

    write_history(rows, HISTORY_PATH)
    print(f"done: +{fetched} rows fetched, {skipped} holiday-skips, {failed} failures; "
          f"table now {len(rows)} rows -> {HISTORY_PATH}")


def main() -> int:
    ap = argparse.ArgumentParser()
    default_from = (_last_completed_trading_day() - timedelta(days=365 * 3)).isoformat()
    ap.add_argument("--from", dest="from_day", default=default_from)
    ap.add_argument("--to", dest="to_day", default=_last_completed_trading_day().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run_backfill(date.fromisoformat(args.from_day),
                             date.fromisoformat(args.to_day),
                             dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
