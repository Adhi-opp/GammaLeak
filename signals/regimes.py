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

The ATM OI RoC inputs (and the OI-flow / GEX fan-out that used to run from
here) are computed by orderflow/chain.py, which runs outside the signal
engine — so blackouts and the CAS freeze no longer pause the chain read.
"""
from __future__ import annotations

from core.config import (
    OI_ROC_CAPITULATION_PCT,
    OI_ROC_PIN_RANGE,
    REGIME_GAMMA_SQUEEZE,
    REGIME_EXPANSION,
    REGIME_PIN,
)
from core.models import SymbolState
from core.state import symbol_states


def classify_dynamic_regime(state: SymbolState) -> str:
    """V4.0 master regime classifier. Determines which of the 3 adaptive regimes
    the market is currently in, based on ATR and the ATM OI RoC.
    Priority: GAMMA SQUEEZE > EXPANSION > THE PIN"""

    nifty_state = symbol_states.get("NSE_INDEX|Nifty 50")
    if nifty_state is None or nifty_state.ltp <= 0:
        return ""

    ce_roc = nifty_state.oi_roc_ce_atm
    pe_roc = nifty_state.oi_roc_pe_atm

    # 1. GAMMA SQUEEZE CHECK (highest priority) — OI capitulation
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
