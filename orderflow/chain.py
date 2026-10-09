"""NIFTY option-chain analytics, decoupled from the cash index and the signal
engine.

Everything here reads the chain against an UNDERLYING level rather than the
cash print directly (`chain_spot`):

  continuous trading    the live NIFTY spot
  CAS window            the futures-implied spot (orderflow.cas.derivatives_spot):
  (CTS end -> F&O       the cash index freezes at 15:15 and then prints the
   close)               auction close, while index futures and options trade
                        on to 15:40 — and the options price off the futures,
                        not the frozen index

Updated here: ATM OI rate-of-change, OI-flow classification + timeline
sample, dealer GEX snapshot, straddle box, vol surface, gamma-flush flag.
Results land on the NIFTY spot SymbolState, as before.

Driven by whichever leg is live (see record_tick): NIFTY spot ticks during
continuous trading, NIFTY_FUT ticks inside the CAS window. It runs outside
update_signal_engine, so warmup, event/expiry blackouts and the CAS freeze
silence signals without stopping the chain read. Before this split the chain
paused with every blackout — including the expiry-day settlement window, the
one stretch the P4 pin study most needs GEX snapshots for.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from core.config import STRADDLE_BOX_ENABLED, VOL_SAMPLE_INTERVAL_SECS, OI_ROC_WINDOW_SECS
from core.models import SymbolState
from core.state import pcr_state, symbol_states
from orderflow.cas import derivatives_spot
from orderflow.gamma import check_gamma_flush
from orderflow.gex import update_gex
from orderflow.oi_flow import classify_oi_flow, record_oi_flow_sample
from signals.vol_regime import update_nifty_vol_state


NIFTY_SPOT_KEY = "NSE_INDEX|Nifty 50"


def chain_spot(now_ist: datetime) -> tuple[float, str]:
    """(level, source) the NIFTY chain is read against — SPOT | FUT | FROZEN."""
    st = symbol_states.get(NIFTY_SPOT_KEY)
    return derivatives_spot("NIFTY", st.ltp if st is not None else 0.0, now_ist)


def atm_strike(underlying: float) -> int:
    return int(round(underlying / 50.0) * 50)


def compute_oi_roc(history: deque, current_ts: float) -> float:
    """% change in OI over the rolling OI_ROC_WINDOW_SECS window."""
    if not history or len(history) < 2:
        return 0.0
    cutoff = current_ts - OI_ROC_WINDOW_SECS
    # Find oldest entry within window
    oldest_oi = None
    for ts, oi in history:
        if ts >= cutoff:
            oldest_oi = oi
            break
    if oldest_oi is None or oldest_oi == 0:
        return 0.0
    newest_oi = history[-1][1]
    return ((newest_oi - oldest_oi) / oldest_oi) * 100


def compute_straddle_box(state: SymbolState, underlying: float) -> tuple[float, float]:
    """Implied upper/lower range from the ATM straddle premium around VWAP.
    ATM follows the chain underlying; the box stays anchored on the cash
    VWAP. Returns (implied_upper, implied_lower), (0, 0) if data is missing."""
    if not STRADDLE_BOX_ENABLED or underlying <= 0 or state.vwap <= 0:
        return 0.0, 0.0

    atm = atm_strike(underlying)
    ce_ltp = pcr_state.ce_ltp_by_strike.get(atm, 0.0)
    pe_ltp = pcr_state.pe_ltp_by_strike.get(atm, 0.0)

    if ce_ltp <= 0 or pe_ltp <= 0:
        return 0.0, 0.0

    premium = ce_ltp + pe_ltp
    state.straddle_premium = premium
    state.implied_upper = state.vwap + (premium / 2)
    state.implied_lower = state.vwap - (premium / 2)
    return state.implied_upper, state.implied_lower


def update_chain_analytics(nifty_state: SymbolState, timestamp: float, now_ist: datetime) -> None:
    """One chain refresh against the current underlying. Internal throttles
    (OI-flow sample 5s, GEX 60s, vol surface 5s) are timestamp-based, so the
    driving leg's tick rate doesn't change their cadence."""
    underlying, _src = chain_spot(now_ist)
    if underlying <= 0:
        return

    atm = atm_strike(underlying)
    nifty_state.oi_roc_ce_atm = compute_oi_roc(pcr_state.oi_history_ce.get(atm, deque()), timestamp)
    nifty_state.oi_roc_pe_atm = compute_oi_roc(pcr_state.oi_history_pe.get(atm, deque()), timestamp)

    classify_oi_flow(nifty_state, underlying)
    record_oi_flow_sample(nifty_state, timestamp)
    update_gex(nifty_state, underlying, timestamp)
    compute_straddle_box(nifty_state, underlying)

    if timestamp - nifty_state._last_vol_surface_ts >= VOL_SAMPLE_INTERVAL_SECS:
        update_nifty_vol_state(nifty_state, timestamp, underlying)
        nifty_state._last_vol_surface_ts = timestamp

    nifty_state.gamma_flush_active, nifty_state.gamma_flush_side = check_gamma_flush(timestamp, underlying)
