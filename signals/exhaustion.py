"""Signal exhaustion + thesis-decay lifecycle.

Three concerns, one module:

  determine_confirmed_signal — once sig_state hits 2, decide whether the
                               confirmed setup is a regular FADE_SCALP or
                               an EXHAUSTION_SCALP (peak_z breached the
                               exhaustion threshold). The exhaustion variant
                               gets bold styling so the trader notices.

  reset_thesis_state         — clear all thesis-age tracking on a fresh start
                               or when the signal exits.

  update_thesis_state        — TTL-based decay: a confirmed thesis older than
                               THESIS_WARN_SECS is marked "decaying" (yellow);
                               at THESIS_HARD_KILL_SECS the engine force-exits
                               the signal (returns sig_state to 0). Prevents
                               a confirmed fade from haunting the dashboard
                               long after its window has closed.
"""
from __future__ import annotations

from core.config import (
    SIGNAL_EXHAUSTION_SCALP_LONG,
    SIGNAL_EXHAUSTION_SCALP_SHORT,
    SIGNAL_FADE_SCALP_LONG,
    SIGNAL_FADE_SCALP_SHORT,
    SIGNAL_NO_EDGE,
    THESIS_HARD_KILL_SECS,
    THESIS_WARN_SECS,
)
from core.models import SymbolState


def determine_confirmed_signal(
    side: int, peak_z: float, exhaustion_peak: float
) -> tuple[str, str]:
    is_exhaustion = abs(peak_z) >= exhaustion_peak
    if side > 0:
        if is_exhaustion:
            return SIGNAL_EXHAUSTION_SCALP_SHORT, "bold red"
        return SIGNAL_FADE_SCALP_SHORT, "red"
    if side < 0:
        if is_exhaustion:
            return SIGNAL_EXHAUSTION_SCALP_LONG, "bold green"
        return SIGNAL_FADE_SCALP_LONG, "green"
    return SIGNAL_NO_EDGE, "dim white"


def reset_thesis_state(state: SymbolState) -> None:
    state.thesis_started_at = None
    state.thesis_age_secs = 0.0
    state.thesis_decay = False
    state.regime_shift_alert = False


def update_thesis_state(state: SymbolState, timestamp: float) -> None:
    if state.sig_state != 2:
        reset_thesis_state(state)
        return

    if state.thesis_started_at is None:
        state.thesis_started_at = timestamp

    state.thesis_age_secs = max(0.0, timestamp - state.thesis_started_at)
    state.thesis_decay = state.thesis_age_secs > THESIS_WARN_SECS

    if state.thesis_age_secs >= THESIS_HARD_KILL_SECS:
        state.sig_state = 0
        state.alert_side = 0
        state.peak_z = 0.0
        state.action_signal = SIGNAL_NO_EDGE
        state.action_style = "dim white"
        state.last_signal_exit_ts = timestamp
        state.conviction_score = 0
        state.setup_label = ""
        reset_thesis_state(state)
