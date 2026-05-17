"""Gamma flush detection.

Fires when three independent micro-structure conditions stack on the ATM strike:
  1. IV spikes by GAMMA_FLUSH_IV_SPIKE_PCT above the 3-min rolling mean.
  2. Gamma expands by GAMMA_FLUSH_GAMMA_EXPAND_PCT above its rolling mean.
  3. Sell-side dominance (TSQ / (TBQ+TSQ)) breaches GAMMA_FLUSH_SELL_DOMINANCE
     across the ATM±100 window.

When all three trip, the OI-unwind side (CE vs PE) decides whether the flush is
upward (+1 — call writers buying back) or downward (-1 — put writers covering).
Consumed by the V3 sig_state override at the engine's `classify_dynamic_regime`
call site, which forces SIGNAL_GAMMA_FLUSH_LONG / SIGNAL_GAMMA_FLUSH_SHORT.
"""
from __future__ import annotations

from core.config import (
    GAMMA_FLUSH_WINDOW_SECS,
    GAMMA_FLUSH_IV_SPIKE_PCT,
    GAMMA_FLUSH_GAMMA_EXPAND_PCT,
    GAMMA_FLUSH_SELL_DOMINANCE,
)
from core.state import symbol_states, pcr_state


def check_gamma_flush(timestamp: float) -> tuple[bool, int]:
    """Check if gamma flush conditions are met across ATM strikes.
    Returns (is_active, side) where side is -1 (downward flush) or +1 (upward flush)."""
    nifty_state = symbol_states.get("NSE_INDEX|Nifty 50")
    if nifty_state is None or nifty_state.ltp <= 0:
        return False, 0

    atm = int(round(nifty_state.ltp / 50.0) * 50)
    cutoff = timestamp - GAMMA_FLUSH_WINDOW_SECS

    # 1. Check IV spike at ATM strike
    iv_deque = pcr_state.iv_history.get(atm)
    if not iv_deque or len(iv_deque) < 10:
        return False, 0
    recent_ivs = [v for t, v in iv_deque if t >= cutoff and v > 0]
    if len(recent_ivs) < 5:
        return False, 0
    mean_iv = sum(recent_ivs) / len(recent_ivs)
    current_iv = recent_ivs[-1]
    if mean_iv <= 0 or (current_iv - mean_iv) / mean_iv < GAMMA_FLUSH_IV_SPIKE_PCT:
        return False, 0

    # 2. Check gamma expansion at ATM strike
    gamma_deque = pcr_state.gamma_history.get(atm)
    if not gamma_deque or len(gamma_deque) < 10:
        return False, 0
    recent_gammas = [v for t, v in gamma_deque if t >= cutoff and v > 0]
    if len(recent_gammas) < 5:
        return False, 0
    mean_gamma = sum(recent_gammas) / len(recent_gammas)
    current_gamma = recent_gammas[-1]
    if mean_gamma <= 0 or (current_gamma - mean_gamma) / mean_gamma < GAMMA_FLUSH_GAMMA_EXPAND_PCT:
        return False, 0

    # 3. Check sell-side dominance across ATM window
    window = [atm - 100, atm - 50, atm, atm + 50, atm + 100]
    total_tbq = sum(pcr_state.tbq_by_strike.get(s, 0) for s in window)
    total_tsq = sum(pcr_state.tsq_by_strike.get(s, 0) for s in window)
    if (total_tbq + total_tsq) <= 0:
        return False, 0
    if total_tsq / (total_tbq + total_tsq) < GAMMA_FLUSH_SELL_DOMINANCE:
        return False, 0

    # All 3 conditions met — determine direction from OI unwind
    pe_oi = sum(pcr_state.pe_oi.get(s, 0) for s in window)
    ce_oi = sum(pcr_state.ce_oi.get(s, 0) for s in window)
    prev_pe = sum(pcr_state.prev_pe_oi.get(s, 0) for s in window)
    prev_ce = sum(pcr_state.prev_ce_oi.get(s, 0) for s in window)
    pe_unwind = prev_pe - pe_oi
    ce_unwind = prev_ce - ce_oi
    side = -1 if pe_unwind > ce_unwind else 1
    return True, side
