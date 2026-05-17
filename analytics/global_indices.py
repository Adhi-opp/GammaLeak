"""Global Indices snapshot service.

Polls Upstox /v3/market-quote/ltp for a curated set of global indices
(GIFT NIFTY, US, Europe, Asia). One batched REST call per refresh; the
result is cached in module state and broadcast to the web dashboard via
the existing serializer path.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import aiohttp


IST = timezone(timedelta(hours=5, minutes=30))

UPSTOX_LTP_URL = "https://api.upstox.com/v3/market-quote/ltp"

# Region grouping is used by the frontend to render sub-tables.
# Order within a region is the render order.
GLOBAL_INDICES: list[tuple[str, str, str]] = [
    # (display, instrument_key, region)
    ("GIFT NIFTY", "GLOBAL_INDEX|SGX NIFTY", "India Lead"),
    ("Dow Jones",  "GLOBAL_INDEX|^DJI",       "US"),
    ("S&P 500",    "GLOBAL_INDEX|^GSPC",      "US"),
    ("Nasdaq 100", "GLOBAL_INDEX|IXIX",       "US"),
    ("FTSE 100",   "GLOBAL_INDEX|^FTSE",      "Europe"),
    ("DAX",        "GLOBAL_INDEX|^GDAXI",     "Europe"),
    ("Nikkei 225", "GLOBAL_INDEX|^N225",      "Asia"),
    ("Hang Seng",  "GLOBAL_INDEX|^HSI",       "Asia"),
]


@dataclass
class GlobalIndexQuote:
    display: str
    region: str
    instrument_key: str
    last_price: float = 0.0
    prev_close: float = 0.0
    fetched_at_ts: float = 0.0

    @property
    def change_abs(self) -> float:
        return self.last_price - self.prev_close

    @property
    def change_pct(self) -> float:
        if self.prev_close == 0.0:
            return 0.0
        return (self.last_price - self.prev_close) / self.prev_close * 100.0


@dataclass
class GlobalIndicesSnapshot:
    quotes: list[GlobalIndexQuote] = field(default_factory=list)
    last_refresh_ts: float = 0.0
    last_error: str = ""

    def to_payload(self) -> dict:
        # Group by region so frontend can render sub-sections in one pass.
        regions: dict[str, list[dict]] = {}
        for q in self.quotes:
            regions.setdefault(q.region, []).append({
                "display": q.display,
                "ltp": round(q.last_price, 2),
                "prev_close": round(q.prev_close, 2),
                "change_abs": round(q.change_abs, 2),
                "change_pct": round(q.change_pct, 2),
            })
        return {
            "regions": regions,
            "last_refresh_ts": self.last_refresh_ts,
            "last_error": self.last_error,
        }


_snapshot = GlobalIndicesSnapshot()


def get_snapshot() -> GlobalIndicesSnapshot:
    return _snapshot


async def fetch_once() -> GlobalIndicesSnapshot:
    # One batched call gets all configured indices. Mutates and returns the
    # module-level snapshot so callers can `await fetch_once()` for the latest
    # or just read `get_snapshot()` between refreshes.
    global _snapshot

    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        _snapshot.last_error = "no_token"
        return _snapshot

    keys = ",".join(k for _, k, _ in GLOBAL_INDICES)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                UPSTOX_LTP_URL,
                params={"instrument_key": keys},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    _snapshot.last_error = f"http_{resp.status}"
                    return _snapshot
                payload = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        _snapshot.last_error = f"{type(exc).__name__}"
        return _snapshot

    data = payload.get("data") or {}
    now_ts = datetime.now(IST).timestamp()

    quotes: list[GlobalIndexQuote] = []
    for display, key, region in GLOBAL_INDICES:
        # Upstox returns entries keyed by instrument_token (same as instrument_key here).
        entry = data.get(key) or {}
        if not entry:
            # Some responses key by the colon-form (rare); try both.
            entry = data.get(key.replace("|", ":")) or {}
        ltp = float(entry.get("last_price", 0) or 0)
        cp = float(entry.get("cp", 0) or 0)
        quotes.append(GlobalIndexQuote(
            display=display,
            region=region,
            instrument_key=key,
            last_price=ltp,
            prev_close=cp,
            fetched_at_ts=now_ts,
        ))

    _snapshot.quotes = quotes
    _snapshot.last_refresh_ts = now_ts
    _snapshot.last_error = ""
    return _snapshot


async def poller_task(interval_secs: float = 30.0) -> None:
    """Long-running task — refresh the snapshot on a fixed cadence.

    30s default cadence: global indices update much slower than NIFTY, and
    we're polling REST not WS, so frequent calls waste quota without value.
    """
    while True:
        try:
            await fetch_once()
        except Exception as exc:
            _snapshot.last_error = f"poller_unexpected:{type(exc).__name__}"
        await asyncio.sleep(interval_secs)
