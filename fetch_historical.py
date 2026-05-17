"""
Upstox Historical Data Fetcher
Fetches historical candle data and saves to CSV in historical/ folder.

Usage:
  python fetch_historical.py                          # Today, all symbols, 1minute
  python fetch_historical.py --date 2026-04-09        # Specific date
  python fetch_historical.py --from 2026-04-01 --to 2026-04-09   # Date range
  python fetch_historical.py --symbol NIFTY           # Single symbol
  python fetch_historical.py --interval 5minute       # 5min candles
  python fetch_historical.py --symbol NIFTY --interval day --from 2026-01-01 --to 2026-04-09
"""

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
BASE_URL = "https://api.upstox.com/v2/historical-candle"

SYMBOLS = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "RELIANCE":  "NSE_EQ|RELIANCE",
    "HDFCBANK":  "NSE_EQ|HDFCBANK",
}

VALID_INTERVALS = ["1minute", "30minute", "day", "week", "month"]

HIST_DIR = Path(__file__).parent / "historical"


def fetch_candles(instrument_key: str, interval: str, from_date: str, to_date: str) -> list:
    encoded_key = quote(instrument_key, safe="")
    url = f"{BASE_URL}/{encoded_key}/{interval}/{to_date}/{from_date}"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code == 429:
        print("  Rate limited, waiting 1s...")
        time.sleep(1)
        resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text[:200]}")
        return []
    data = resp.json()
    candles = data.get("data", {}).get("candles", [])
    return candles


def save_csv(candles: list, symbol_name: str, interval: str, from_date: str, to_date: str):
    if not candles:
        print(f"  No data for {symbol_name}")
        return None

    HIST_DIR.mkdir(exist_ok=True)

    if from_date == to_date:
        filename = f"{symbol_name}_{interval}_{from_date}.csv"
    else:
        filename = f"{symbol_name}_{interval}_{from_date}_to_{to_date}.csv"

    filepath = HIST_DIR / filename

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "oi"])
        for candle in sorted(candles, key=lambda c: c[0]):
            writer.writerow(candle)

    print(f"  Saved {len(candles)} candles -> {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Fetch Upstox historical candle data")
    parser.add_argument("--symbol", type=str, default=None,
                        help=f"Symbol to fetch. Options: {', '.join(SYMBOLS.keys())} or 'ALL' (default: ALL)")
    parser.add_argument("--date", type=str, default=None,
                        help="Single date (YYYY-MM-DD). Default: today")
    parser.add_argument("--from", dest="from_date", type=str, default=None,
                        help="Start date for range (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="End date for range (YYYY-MM-DD)")
    parser.add_argument("--interval", type=str, default="1minute",
                        help=f"Candle interval. Options: {', '.join(VALID_INTERVALS)} (default: 1minute)")
    parser.add_argument("--key", type=str, default=None,
                        help="Custom instrument key (e.g. 'MCX_FO|CRUDEOIL26APRFUT')")
    args = parser.parse_args()

    if not ACCESS_TOKEN:
        print("ERROR: UPSTOX_ACCESS_TOKEN not set in .env")
        sys.exit(1)

    if args.interval not in VALID_INTERVALS:
        print(f"ERROR: Invalid interval '{args.interval}'. Use one of: {', '.join(VALID_INTERVALS)}")
        sys.exit(1)

    # Resolve dates
    if args.date:
        from_date = to_date = args.date
    elif args.from_date and args.to_date:
        from_date = args.from_date
        to_date = args.to_date
    elif args.from_date:
        from_date = args.from_date
        to_date = date.today().isoformat()
    else:
        from_date = to_date = date.today().isoformat()

    # Resolve symbols
    if args.key:
        # Custom key — use the last part as name
        name = args.key.split("|")[-1].replace(" ", "_")
        targets = {name: args.key}
    elif args.symbol and args.symbol.upper() != "ALL":
        sym = args.symbol.upper()
        if sym not in SYMBOLS:
            print(f"ERROR: Unknown symbol '{sym}'. Available: {', '.join(SYMBOLS.keys())}")
            print(f"  Use --key for custom instruments (e.g. --key 'MCX_FO|CRUDEOIL26APRFUT')")
            sys.exit(1)
        targets = {sym: SYMBOLS[sym]}
    else:
        targets = dict(SYMBOLS)

    print(f"Fetching {args.interval} candles from {from_date} to {to_date}")
    print(f"Symbols: {', '.join(targets.keys())}")
    print(f"Output:  {HIST_DIR}/")
    print()

    for name, key in targets.items():
        print(f"[{name}] {key}")
        candles = fetch_candles(key, args.interval, from_date, to_date)
        save_csv(candles, name, args.interval, from_date, to_date)
        if len(targets) > 1:
            time.sleep(0.3)  # gentle rate limit

    print("\nDone.")


if __name__ == "__main__":
    main()
