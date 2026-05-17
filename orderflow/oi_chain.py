"""Full-chain OI snapshot service.

Polls Upstox /v2/market/oi periodically and exposes:
  - Full-chain Max Pain (computed across all ~110 strikes, not just ATM±500)
  - Dynamic near-ATM walls (window recomputed each query relative to LIVE spot)
  - Deep OI Clusters (significant OI beyond the near-ATM window)

The WS path already gives per-tick OI for ATM±500. This module is purely
supplementary — wider chain, no per-strike subscription cost.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable

import aiohttp


IST = timezone(timedelta(hours=5, minutes=30))

UPSTOX_OI_URL = "https://api.upstox.com/v2/market/oi"


@dataclass
class FullChainOI:
    instrument_key: str
    expiry: str  # YYYY-MM-DD
    as_of_date: str  # YYYY-MM-DD the API was queried for
    spot_closing_price: float
    total_calls: int
    total_puts: int
    # strike (float) -> (call_oi, put_oi)
    strikes: dict[float, tuple[int, int]] = field(default_factory=dict)
    fetched_at_ts: float = 0.0

    @property
    def n_strikes(self) -> int:
        return len(self.strikes)

    @property
    def pcr(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_puts / self.total_calls


@dataclass
class OIChainSnapshot:
    chain: FullChainOI | None = None
    last_refresh_ts: float = 0.0
    last_error: str = ""

    def is_fresh(self, max_age_secs: float = 180.0) -> bool:
        if self.chain is None or self.last_refresh_ts == 0:
            return False
        age = datetime.now(IST).timestamp() - self.last_refresh_ts
        return age < max_age_secs


_snapshot = OIChainSnapshot()


def get_snapshot() -> OIChainSnapshot:
    return _snapshot


def compute_max_pain(strikes: dict[float, tuple[int, int]]) -> tuple[int, float]:
    """Return (max_pain_strike, min_total_pain).

    Standard max pain: at expiry, total option holder loss at price S equals
        sum_over_call_strikes(call_oi[k] * max(S - k, 0))
      + sum_over_put_strikes(put_oi[k]  * max(k - S, 0))
    The strike that MINIMIZES this is "max pain" — where writers earn most.
    """
    if not strikes:
        return 0, 0.0
    strike_list = sorted(strikes.keys())
    best_strike = strike_list[0]
    best_pain = float("inf")
    for s in strike_list:
        pain = 0.0
        for k, (ce_oi, pe_oi) in strikes.items():
            if s > k:
                pain += ce_oi * (s - k)
            if s < k:
                pain += pe_oi * (k - s)
        if pain < best_pain:
            best_pain = pain
            best_strike = s
    return int(best_strike), best_pain


def near_atm_walls(
    strikes: dict[float, tuple[int, int]],
    spot: float,
    n_strikes_each_side: int = 5,
) -> dict:
    """Pick the (2*N + 1) strikes closest to LIVE spot and return their OI.

    Window is recomputed every call from `spot`, so as price moves intraday
    the displayed walls follow. Returns the full window so the frontend can
    sort/style as it wishes.
    """
    if not strikes:
        return {"window": [], "atm": 0, "spot": spot}
    sorted_strikes = sorted(strikes.keys())
    # Pick ATM = strike closest to spot
    atm = min(sorted_strikes, key=lambda k: abs(k - spot))
    atm_idx = sorted_strikes.index(atm)
    lo = max(0, atm_idx - n_strikes_each_side)
    hi = min(len(sorted_strikes), atm_idx + n_strikes_each_side + 1)
    window_strikes = sorted_strikes[lo:hi]
    window = []
    for k in window_strikes:
        ce_oi, pe_oi = strikes[k]
        window.append({
            "strike": int(k) if k.is_integer() else k,
            "call_oi": ce_oi,
            "put_oi": pe_oi,
            "is_atm": k == atm,
            "dist_pct": round((k - spot) / spot * 100.0, 3) if spot else 0.0,
        })
    return {"window": window, "atm": int(atm) if atm.is_integer() else atm, "spot": spot}


def deep_clusters(
    strikes: dict[float, tuple[int, int]],
    spot: float,
    excluded_window: int = 5,
    top_n: int = 3,
) -> dict:
    """Significant OI strikes that sit OUTSIDE the near-ATM window.

    Surfaces things like a 22,500 PE wall when spot is at 23,800 — the kind of
    far-OTM hedge level that ATM±500 (the WS subscription scope) misses.
    """
    if not strikes:
        return {"top_calls": [], "top_puts": []}
    sorted_strikes = sorted(strikes.keys())
    atm = min(sorted_strikes, key=lambda k: abs(k - spot))
    atm_idx = sorted_strikes.index(atm)
    lo = max(0, atm_idx - excluded_window)
    hi = min(len(sorted_strikes), atm_idx + excluded_window + 1)
    excluded = set(sorted_strikes[lo:hi])

    far_strikes = [k for k in sorted_strikes if k not in excluded]
    # Top call OI outside window
    top_calls = sorted(
        [(k, strikes[k][0]) for k in far_strikes if strikes[k][0] > 0],
        key=lambda x: x[1],
        reverse=True,
    )[:top_n]
    # Top put OI outside window
    top_puts = sorted(
        [(k, strikes[k][1]) for k in far_strikes if strikes[k][1] > 0],
        key=lambda x: x[1],
        reverse=True,
    )[:top_n]

    def fmt(items):
        return [
            {
                "strike": int(k) if k.is_integer() else k,
                "oi": oi,
                "dist_pct": round((k - spot) / spot * 100.0, 3) if spot else 0.0,
            }
            for k, oi in items
        ]
    return {"top_calls": fmt(top_calls), "top_puts": fmt(top_puts)}


async def fetch_full_chain_oi(
    instrument_key: str, expiry: str, date_str: str
) -> FullChainOI | None:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        return None

    params = {
        "instrument_key": instrument_key,
        "expiry": expiry,
        "date": date_str,
    }
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                UPSTOX_OI_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None

    data = payload.get("data")
    if not data:
        return None

    strike_rows = data.get("call_put_oi_data_list") or []
    strikes: dict[float, tuple[int, int]] = {}
    for row in strike_rows:
        try:
            strike = float(row.get("strike_price", 0) or 0)
            if strike <= 0:
                continue
            ce_oi = int(row.get("call_oi", 0) or 0)
            pe_oi = int(row.get("put_oi", 0) or 0)
            strikes[strike] = (ce_oi, pe_oi)
        except (TypeError, ValueError):
            continue

    if not strikes:
        return None

    return FullChainOI(
        instrument_key=instrument_key,
        expiry=expiry,
        as_of_date=date_str,
        spot_closing_price=float(data.get("spot_closing_price", 0) or 0),
        total_calls=int(data.get("total_calls", 0) or 0),
        total_puts=int(data.get("total_puts", 0) or 0),
        strikes=strikes,
        fetched_at_ts=datetime.now(IST).timestamp(),
    )


async def poller_task(
    resolve_params: Callable[[], Awaitable[tuple[str, str, str] | None]],
    interval_secs: float = 60.0,
) -> None:
    """Long-running task — refresh full-chain OI on `interval_secs` cadence.

    `resolve_params` is async and returns (instrument_key, expiry_YYYY-MM-DD,
    date_YYYY-MM-DD), or None if the engine isn't ready yet. Pulling these
    fresh each tick lets the poller pick up daily expiry rollover without
    a restart.
    """
    global _snapshot
    while True:
        try:
            params = await resolve_params()
            if params is None:
                _snapshot.last_error = "params_unresolved"
            else:
                instrument_key, expiry, date_str = params
                chain = await fetch_full_chain_oi(instrument_key, expiry, date_str)
                if chain is None:
                    _snapshot.last_error = "fetch_failed"
                else:
                    _snapshot.chain = chain
                    _snapshot.last_refresh_ts = datetime.now(IST).timestamp()
                    _snapshot.last_error = ""
        except Exception as exc:
            _snapshot.last_error = f"poller_unexpected:{type(exc).__name__}"
        await asyncio.sleep(interval_secs)
