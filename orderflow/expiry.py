"""Expiry settlement-anchor + pin-strike tracker (research track P4, shadow).

India-specific mechanics, pre-CAS: NIFTY weekly options settled to the
exchange-computed average of the underlying index over the last half hour of
expiry day. From 15:00 every passing tick LOCKED IN part of that settlement
price, so writers defending a strike boundary faced a shrinking-uncertainty
problem and their hedging became progressively more deterministic.

Since 2026-08-03 (CAS) the settlement is the single CAS-derived index close:
nothing locks in progressively — the index freezes at 15:15 and one auction
print (15:28-15:30) fixes the settlement while expiring contracts trade on
to 15:40. On CAS days settle_est follows orderflow/cas.py's implied close and
then the printed close; the two eras are different mechanisms and must not
be pooled in the P4 study. This module measures:

  settle_est — pre-CAS: running mean of spot ticks inside the settlement
               window (a proxy for the official average). CAS days: spot
               before the freeze, then the auction's implied close
               (SYNTH/FUT), then the official close (see settle_src)
  pin strike — the strike with maximum gamma MASS, γ(K)·(OI_CE+OI_PE): total
               hedging concentration, sign-agnostic (pinning force does not
               care which side dealers are on). Falls back to the pure-OI
               wall when the chain hasn't delivered live greeks (pin_src=OI).

Observational only: writes state fields + logs/<date>_expiry.csv rows.
Nothing gates on this. Validation per proposal P4 needs several expiries of
collected data (event study vs matched non-expiry afternoons).

Called from record_tick on every NIFTY spot tick — including frozen CAS
ticks — and deliberately NOT from the signal engine, because the expiry
blackout short-circuits update_signal_engine during exactly the window this
exists to observe. Restart caveat: the pre-CAS accumulator lives in module
state, so an engine restart inside the window restarts the average.
"""
from __future__ import annotations

from datetime import datetime

from core.config import (
    IST,
    CAS_MATCHING_START,
    EXPIRY_TRACK_START,
    EXPIRY_SETTLE_START,
    EXPIRY_SETTLE_END,
    EXPIRY_SAMPLE_SECS_PRE,
    EXPIRY_SAMPLE_SECS_SETTLE,
    FNO_CLOSE,
    cas_active,
)
from core.models import SymbolState
from core.state import pcr_state
from orderflow.cas import settlement_estimate
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
    cas = cas_active(ts_ist.date())
    window_end = FNO_CLOSE if cas else EXPIRY_SETTLE_END
    # When the settlement price gets fixed: the auction print (no later than
    # matching start) under CAS, the end of the averaging window before it.
    fix_at = CAS_MATCHING_START if cas else EXPIRY_SETTLE_END
    if cur < _mins(EXPIRY_TRACK_START) or cur >= _mins(window_end):
        return

    day_key = ts_ist.date().isoformat()
    if _settle_day != day_key:
        _settle_day = day_key
        _settle_sum = 0.0
        _settle_n = 0
    in_window = cur >= _mins(EXPIRY_SETTLE_START)
    settle_src = ""
    if in_window and cas:
        est = settlement_estimate("NIFTY", ts_ist)
        if est is None:                          # before the freeze: spot is still live
            nifty_state.expiry_settle_est, settle_src = nifty_state.ltp, "SPOT"
        else:
            nifty_state.expiry_settle_est, settle_src = est
    elif in_window:
        # Pre-CAS settlement accumulator — every tick inside the window counts.
        _settle_sum += nifty_state.ltp
        _settle_n += 1
        nifty_state.expiry_settle_est = _settle_sum / _settle_n
        settle_src = "MEAN"

    # Snapshot cadence: coarse before the window, fine inside it.
    cadence = EXPIRY_SAMPLE_SECS_SETTLE if in_window else EXPIRY_SAMPLE_SECS_PRE
    if timestamp - _last_sample_ts < cadence:
        return
    _last_sample_ts = timestamp

    # Pin distance is measured from where settlement is heading: on a CAS
    # day inside the window that is settle_est (implied, then printed
    # close), not the frozen cash value.
    ref = nifty_state.expiry_settle_est if (cas and in_window) else nifty_state.ltp
    pin = find_pin_strike(timestamp)
    if pin is not None:
        strike, src, mass = pin
        nifty_state.expiry_pin_strike = strike
        nifty_state.expiry_pin_dist = ref - strike
    else:
        strike, src, mass = 0, "", 0.0

    mins_to_close = max(0.0, _mins(fix_at) - (cur + ts_ist.second / 60.0))
    try:
        append_expiry_row(
            timestamp=timestamp,
            spot=nifty_state.ltp,
            settle_est=(nifty_state.expiry_settle_est if in_window else None),
            settle_n=_settle_n,
            pin_strike=strike, pin_src=src, pin_mass=mass,
            pin_dist=(ref - strike) if strike else None,
            mins_to_close=mins_to_close,
            settle_src=settle_src,
        )
    except Exception:
        pass  # disk errors never propagate into the tick path
