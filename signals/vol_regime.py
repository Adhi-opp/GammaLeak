"""Vol regime classifier — labels for the NIFTY SymbolState vol surface fields.

Derives two coarse labels from the surface metrics computed by analytics/vol_surface.py:

  IV_REGIME  : LOW_IV | NORMAL_IV | HIGH_IV
  SKEW_STATE : FEAR | NEUTRAL | COMPLACENCY

These are consumed in two ways:
  1. Dashboard display — IV chip and skew chip on the NIFTY card
  2. Conviction factor — VOL fires in LOW_IV + non-FEAR conditions (weight 0
     until calibrate.py has measured lift; kept observational for now)

Thresholds are intentionally coarse. With 30 sessions of history the
distribution will naturally tighten; do not over-tune on fewer than 20 points.
"""
from __future__ import annotations

import time

from analytics.vol_surface import (
    VOL_SAMPLE_INTERVAL_SECS,
    get_atm_iv,
    get_skew,
    get_iv_percentile,
    load_history,
    maybe_record_session_iv,
)
from core.state import pcr_state

# IV percentile thresholds
VOL_IV_LOW_PCT: float = 30.0   # below = LOW_IV (historically cheap vol, fades clean)
VOL_IV_HIGH_PCT: float = 70.0  # above = HIGH_IV (fear, wider true ranges)

# Skew thresholds (IV is decimal — 0.02 = 2pp difference between OTM put and call)
VOL_SKEW_FEAR: float = 0.02    # put premium >2pp = institutional hedging = FEAR
VOL_SKEW_CALM: float = 0.005   # put premium <0.5pp = complacency or call-side bid


def classify_iv_regime(pct: float) -> str:
    if pct < VOL_IV_LOW_PCT:
        return "LOW_IV"
    if pct > VOL_IV_HIGH_PCT:
        return "HIGH_IV"
    return "NORMAL_IV"


def classify_skew_state(skew: float) -> str:
    if skew > VOL_SKEW_FEAR:
        return "FEAR"
    if skew < VOL_SKEW_CALM:
        return "COMPLACENCY"
    return "NEUTRAL"


def update_nifty_vol_state(state, timestamp: float) -> None:
    """Compute and stamp vol surface metrics onto the NIFTY SymbolState.

    Throttled to VOL_SAMPLE_INTERVAL_SECS by the caller — this function does
    no throttling itself so it can be tested cleanly.

    Sets on state: atm_iv, skew_25d, iv_percentile, vol_regime, skew_state.
    All fields default to safe neutral values when data is absent.
    """
    if state.ltp <= 0.0:
        return

    now = timestamp
    ce_hist = pcr_state.iv_history_ce
    pe_hist = pcr_state.iv_history_pe

    atm_iv = get_atm_iv(state.ltp, now, ce_hist, pe_hist)
    if atm_iv is None:
        return  # no live IV data yet — keep previous values

    state.atm_iv = atm_iv

    skew = get_skew(state.ltp, now, ce_hist, pe_hist)
    if skew is not None:
        state.skew_25d = skew
        state.skew_state = classify_skew_state(skew)

    history = load_history()
    iv_pct = get_iv_percentile(atm_iv, history)
    state.iv_percentile = iv_pct
    state.vol_regime = classify_iv_regime(iv_pct)

    if state.session_day is not None:
        maybe_record_session_iv(atm_iv, state.session_day.isoformat(), now)
