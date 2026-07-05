"""Expiry settlement-anchor + pin-strike tracker (research track P4, shadow).

India-specific mechanics: NIFTY weekly options settle to the exchange-computed
average of the underlying index over the last half hour of expiry day. From
15:00 every passing tick LOCKS IN part of that settlement price, so writers
defending a strike boundary face a shrinking-uncertainty problem and their
hedging becomes progressively more deterministic. This module measures the
two quantities that hypothesis needs:

  settle_est — running mean of spot ticks inside the settlement window (an
               estimate of the official average; the exchange's exact
               weighting may differ slightly — treat as a proxy)
  pin strike — the strike with maximum gamma MASS, γ(K)·(OI_CE+OI_PE): total
               hedging concentration, sign-agnostic (pinning force does not
               care which side dealers are on). Falls back to the pure-OI
               wall when the chain hasn't delivered live greeks (pin_src=OI).

Observational only: writes state fields + logs/<date>_expiry.csv rows.
Nothing gates on this. Validation per proposal P4 needs several expiries of
collected data (event study vs matched non-expiry afternoons).

Called from record_tick on every NIFTY spot tick — deliberately NOT from the
signal engine, because the existing 15:00–15:30 expiry blackout short-circuits
update_signal_engine during exactly the window this exists to observe.
Restart caveat: the settlement accumulator lives in module state, so an
engine restart inside the window restarts the average from that point.
"""
from __future__ import annotations

from datetime import datetime

from core.config import (
    IST,
    EXPIRY_TRACK_START,
    EXPIRY_SETTLE_START,
    EXPIRY_SETTLE_END,
    EXPIRY_SAMPLE_SECS_PRE,
    EXPIRY_SAMPLE_SECS_SETTLE,
)
from core.models import SymbolState
from core.state import pcr_state
from orderflow.gex import _latest_gamma
from gammaleak_runtime.io_logs import append_expiry_row


_settle_day: str = ""       # ISO date the accumulator belongs to
_settle_sum: float = 0.0
_settle_n: int = 0
_last_sample_ts: float = 0.0


def _mins(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def find_pin_strike(now: float) -> tuple[int, str, float] | None:
    """(strike, source, mass) for the max gamma-mass strike, or None.

    source = "GEX" when live greeks priced the mass, "OI" when only OI did.
    """
    best_gex: tuple[float, int] | None = None
    best_oi: tuple[float, int] | None = None
    for strike in set(pcr_state.ce_oi) | set(pcr_state.pe_oi):
        total_oi = float(pcr_state.ce_oi.get(strike, 0.0)) + float(pcr_state.pe_oi.get(strike, 0.0))
        if total_oi <= 0.0:
            continue
        if best_oi is None or total_oi > best_oi[0]:
            best_oi = (total_oi, strike)
        g = _latest_gamma(pcr_state.gamma_history.get(strike), now)
        if g is not None:
            mass = g * total_oi
            if best_gex is None or mass > best_gex[0]:
                best_gex = (mass, strike)
    if best_gex is not None:
        return best_gex[1], "GEX", best_gex[0]
    if best_oi is not None:
        return best_oi[1], "OI", best_oi[0]
    return None


def update_expiry_anchor(nifty_state: SymbolState, timestamp: float) -> None:
    """Per-NIFTY-tick expiry tracking. No-op on non-expiry sessions."""
    global _settle_day, _settle_sum, _settle_n, _last_sample_ts

    # Expiry gate — resolved date only (no weekday fallback here: an
    # unresolved master also means no chain, hence no pin to track).
    from analytics.vol_surface import get_expiry_date  # lazy, matches vol_regime.py
    expiry = get_expiry_date()
    if expiry is None or nifty_state.session_day != expiry:
        return
    if nifty_state.ltp <= 0:
        return

    ts_ist = datetime.fromtimestamp(timestamp, IST)
    cur = ts_ist.hour * 60 + ts_ist.minute
    if cur < _mins(EXPIRY_TRACK_START) or cur >= _mins(EXPIRY_SETTLE_END):
        return

    # Settlement accumulator — every tick inside the window counts.
    day_key = ts_ist.date().isoformat()
    if _settle_day != day_key:
        _settle_day = day_key
        _settle_sum = 0.0
        _settle_n = 0
    in_window = cur >= _mins(EXPIRY_SETTLE_START)
    if in_window:
        _settle_sum += nifty_state.ltp
        _settle_n += 1
        nifty_state.expiry_settle_est = _settle_sum / _settle_n

    # Snapshot cadence: coarse before the window, fine inside it.
    cadence = EXPIRY_SAMPLE_SECS_SETTLE if in_window else EXPIRY_SAMPLE_SECS_PRE
    if timestamp - _last_sample_ts < cadence:
        return
    _last_sample_ts = timestamp

    pin = find_pin_strike(timestamp)
    if pin is not None:
        strike, src, mass = pin
        nifty_state.expiry_pin_strike = strike
        nifty_state.expiry_pin_dist = nifty_state.ltp - strike
    else:
        strike, src, mass = 0, "", 0.0

    mins_to_close = max(0.0, _mins(EXPIRY_SETTLE_END) - (cur + ts_ist.second / 60.0))
    try:
        append_expiry_row(
            timestamp=timestamp,
            spot=nifty_state.ltp,
            settle_est=(nifty_state.expiry_settle_est if in_window else None),
            settle_n=_settle_n,
            pin_strike=strike, pin_src=src, pin_mass=mass,
            pin_dist=(nifty_state.ltp - strike) if strike else None,
            mins_to_close=mins_to_close,
        )
    except Exception:
        pass  # disk errors never propagate into the tick path
