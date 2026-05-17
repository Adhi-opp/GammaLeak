"""V5.2 Micro-structural momentum layer.

Four per-symbol updaters that surface pre-move signals the heavy session VWAP
swallows by mid-morning:

  update_micro_z      — 15-min rolling Z (de-anchored from session VWAP)
  update_z_velocity   — dZ/dt across ZVEL_WINDOW_SECS (Z-score acceleration)
  update_amber_state  — early-warning amber tier, fires before sig_state hits 1
  update_tick_rate    — tick-arrival spike vs trailing 10-min baseline

All four are pure per-symbol mutators. They read state fields + module config,
write back to state. No engine globals required.
"""
from __future__ import annotations

import numpy as np

from core.config import (
    DEQUE_MAXLEN,
    MICRO_Z_MIN_POINTS,
    MICRO_Z_WINDOW_SECS,
    MICRO_Z_SD_ATR_FRAC,
    SD_MIN_ABSOLUTE,
    Z_SCORE_CAP,
    ZVEL_WINDOW_SECS,
    ZVEL_MIN_Z,
    ZVEL_MAX_Z,
    ZVEL_AMBER_THRESHOLD,
    TICKRATE_SHORT_SECS,
    TICKRATE_BASELINE_SECS,
    TICKRATE_SPIKE_MULT,
    TICKRATE_MIN_BASELINE_HZ,
)
from core.models import SymbolState


def update_micro_z(state: SymbolState, now: float) -> None:
    """Layer 1: rolling 15-min mean/σ/Z — de-anchors from heavy session VWAP.

    By ~10:30 AM the session VWAP/σ have accumulated enough to swallow a
    10-15pt flush (|session_Z| barely moves). This secondary anchor resets
    every 15min so localized stretches remain visible.
    """
    n = min(state._tick_count, DEQUE_MAXLEN)
    if n < MICRO_Z_MIN_POINTS:
        state.micro_vwap = 0.0
        state.micro_std_dev = 0.0
        state.micro_z_score = 0.0
        return

    if n < DEQUE_MAXLEN:
        ts = state._timestamps[:n]
        pr = state._prices[:n]
    else:
        start = state._tick_count % DEQUE_MAXLEN
        ts = np.concatenate((state._timestamps[start:], state._timestamps[:start]))
        pr = np.concatenate((state._prices[start:], state._prices[:start]))

    cutoff = now - MICRO_Z_WINDOW_SECS
    window = pr[ts >= cutoff]
    if window.size < MICRO_Z_MIN_POINTS:
        state.micro_vwap = 0.0
        state.micro_std_dev = 0.0
        state.micro_z_score = 0.0
        return

    m = float(np.mean(window))
    sd = float(np.std(window))
    sd_floor = SD_MIN_ABSOLUTE
    if state.atr > 0:
        sd_floor = max(sd_floor, state.atr * MICRO_Z_SD_ATR_FRAC)

    state.micro_vwap = m
    state.micro_std_dev = max(sd, sd_floor)
    raw = (state.ltp - m) / state.micro_std_dev if state.micro_std_dev > 0 else 0.0
    state.micro_z_score = float(np.clip(raw, -Z_SCORE_CAP, Z_SCORE_CAP))


def update_z_velocity(state: SymbolState, now: float) -> None:
    """Layer 2a: dZ/dt over ZVEL_WINDOW_SECS (z-score acceleration)."""
    state._z_history.append((now, state.z_score))
    cutoff = now - ZVEL_WINDOW_SECS
    while state._z_history and state._z_history[0][0] < cutoff:
        state._z_history.popleft()

    if len(state._z_history) < 2:
        state.z_velocity = 0.0
        return

    t0, z0 = state._z_history[0]
    dt = now - t0
    if dt <= 0:
        state.z_velocity = 0.0
        return

    state.z_velocity = (state.z_score - z0) / dt


def update_amber_state(state: SymbolState, now: float) -> None:
    """Layer 2b: amber pre-alert — Z still inside ±2 but velocity driving further extreme.

    Amber is an attention-only tier BELOW sig_state. When sig_state≥1 the
    full alert subsumes amber. Amber fires when:
      - |Z| ∈ [ZVEL_MIN_Z, ZVEL_MAX_Z), AND
      - |dZ/dt| ≥ ZVEL_AMBER_THRESHOLD, AND
      - Z and dZ/dt share sign (magnitude is growing, not reverting).

    Driver-amber (set by refresh_driver_acceleration) can ALSO raise amber
    on NIFTY specifically, with amber_reason='DRIVER'. Driver takes priority
    over ZVEL if both would fire — driver is the earlier signal.
    """
    # Full alert takes over — clear amber
    if state.sig_state >= 1:
        state.amber_active = False
        state.amber_side = 0
        state.amber_reason = ""
        return

    abs_z = abs(state.z_score)
    abs_vel = abs(state.z_velocity)

    if abs_z < ZVEL_MIN_Z or abs_z >= ZVEL_MAX_Z:
        # Don't clear if a DRIVER amber was just set externally this tick
        if state.amber_reason != "DRIVER":
            state.amber_active = False
            state.amber_side = 0
            state.amber_reason = ""
        return

    # Same-sign Z and velocity = magnitude is growing
    if abs_vel >= ZVEL_AMBER_THRESHOLD and (state.z_score * state.z_velocity > 0):
        state.amber_active = True
        state.amber_side = 1 if state.z_score > 0 else -1
        state.amber_reason = "ZVEL"
    elif state.amber_reason != "DRIVER":
        state.amber_active = False
        state.amber_side = 0
        state.amber_reason = ""


def update_tick_rate(state: SymbolState, now: float) -> None:
    """Layer 3: tick-arrival spike — ≥2× trailing 10-min baseline = 'something happening now'."""
    n = min(state._tick_count, DEQUE_MAXLEN)
    if n < 10:
        state.tick_rate_short = 0.0
        state.tick_rate_baseline = 0.0
        state.tick_rate_spike = False
        return

    if n < DEQUE_MAXLEN:
        ts = state._timestamps[:n]
    else:
        start = state._tick_count % DEQUE_MAXLEN
        ts = np.concatenate((state._timestamps[start:], state._timestamps[:start]))

    short_cut = now - TICKRATE_SHORT_SECS
    base_cut = now - TICKRATE_BASELINE_SECS
    short_n = int(np.sum(ts >= short_cut))

    base_ts = ts[ts >= base_cut]
    if base_ts.size < 2:
        state.tick_rate_short = short_n / TICKRATE_SHORT_SECS
        state.tick_rate_baseline = 0.0
        state.tick_rate_spike = False
        return

    base_span = max(now - float(base_ts[0]), 1.0)
    state.tick_rate_short = short_n / TICKRATE_SHORT_SECS
    state.tick_rate_baseline = base_ts.size / base_span

    if state.tick_rate_baseline >= TICKRATE_MIN_BASELINE_HZ:
        state.tick_rate_spike = (
            state.tick_rate_short >= state.tick_rate_baseline * TICKRATE_SPIKE_MULT
        )
    else:
        state.tick_rate_spike = False
