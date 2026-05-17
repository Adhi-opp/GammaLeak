"""V4.0 Adaptive regime classifier.

Determines which of three adaptive regimes the market is in:

  GAMMA SQUEEZE (highest priority) — OI capitulation at ATM (CE or PE OI RoC
                                      breaches -OI_ROC_CAPITULATION_PCT)
  EXPANSION                        — current std-dev > 2× ATR, OR ATR ratio > 1.5
  THE PIN                          — flat OI RoC (writers comfortable), default
                                      mean-reversion box

Returns the regime label (one of the constants from core.config) or empty
string if no decisive read. The engine's `update_signal_engine` consumes the
return value to tag the dashboard and gate fade signals.

Side effect: also runs `classify_oi_flow` + `record_oi_flow_sample` once per
NIFTY tick — the regime classifier is the natural place to fan these out
since it already runs on every spot tick.
"""
from __future__ import annotations

from collections import deque

from core.config import (
    OI_ROC_WINDOW_SECS,
    OI_ROC_CAPITULATION_PCT,
    OI_ROC_PIN_RANGE,
    REGIME_GAMMA_SQUEEZE,
    REGIME_EXPANSION,
    REGIME_PIN,
)
from core.models import SymbolState
from core.state import symbol_states, pcr_state
from orderflow.oi_flow import classify_oi_flow, record_oi_flow_sample


def compute_oi_roc(history: deque, current_ts: float) -> float:
    """Compute % change in OI over the rolling OI_ROC_WINDOW_SECS window."""
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


def classify_dynamic_regime(state: SymbolState, timestamp: float) -> str:
    """V4.0 master regime classifier. Determines which of the 3 adaptive regimes
    the market is currently in, based on ATR, OI RoC, and straddle box.
    Priority: GAMMA SQUEEZE > EXPANSION > THE PIN"""

    nifty_state = symbol_states.get("NSE_INDEX|Nifty 50")
    if nifty_state is None or nifty_state.ltp <= 0:
        return ""

    atm = int(round(nifty_state.ltp / 50.0) * 50)

    # 1. GAMMA SQUEEZE CHECK (highest priority) — OI capitulation
    ce_roc = compute_oi_roc(pcr_state.oi_history_ce.get(atm, deque()), timestamp)
    pe_roc = compute_oi_roc(pcr_state.oi_history_pe.get(atm, deque()), timestamp)

    # Store on Nifty state for dashboard visibility
    nifty_state.oi_roc_ce_atm = ce_roc
    nifty_state.oi_roc_pe_atm = pe_roc

    # V5.1: OI Delta flow classification (runs on every regime tick)
    classify_oi_flow(nifty_state)
    record_oi_flow_sample(nifty_state, timestamp)

    if ce_roc <= OI_ROC_CAPITULATION_PCT or pe_roc <= OI_ROC_CAPITULATION_PCT:
        return REGIME_GAMMA_SQUEEZE

    # 2. EXPANSION CHECK — ATR blowing out vs session mean
    # If current std_dev is 2x the ATR, the "box" is broken
    if state.atr > 0 and state.std_dev > (state.atr * 2.0):
        return REGIME_EXPANSION

    # Also check: ATR ratio > 1.5 means volatility is expanding fast
    if state.atr_ratio > 1.5:
        return REGIME_EXPANSION

    # 3. THE PIN — default mean-reversion regime
    # Confirmed when OI RoC is flat (writers comfortable holding)
    if (OI_ROC_PIN_RANGE[0] <= ce_roc <= OI_ROC_PIN_RANGE[1]
            and OI_ROC_PIN_RANGE[0] <= pe_roc <= OI_ROC_PIN_RANGE[1]):
        return REGIME_PIN

    return ""  # No strong regime signal — defer to existing V3 logic
