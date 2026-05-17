"""Lee-Ready aggressor classifier, cumulative volume delta accumulator,
and the five CVD-vs-price divergence detectors.

All functions take a `SymbolState`-shaped object and mutate it in place. The
state class is defined in `GammaLeak.py` and is duck-typed here — we
only read / write the field names listed in each function's docstring.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from GammaLeak import SymbolState


# Gap bucket thresholds (percent). Chosen so a typical no-news open lands in FLAT.
GAP_LARGE_PCT = 0.60
GAP_SMALL_PCT = 0.15

# Flow / CVD thresholds — calibrated for NIFTY / BN front-month futures volumes.
# Tune per-symbol later from live logs.
CVD_DIVERGENCE_DELTA_MIN = 5000    # cumulative CVD swing vs prior swing-extreme CVD to trigger exhaustion
CVD_BREAKOUT_MIN_DELTA = 1500      # 1-min Δ-CVD threshold to confirm an OR breakout
CVD_ABSORPTION_5MIN_MIN = 3000     # 5-min cumulative Δ-CVD threshold for absorption while price is flat
CVD_ABSORPTION_ER_MAX = 0.30       # ER must be below this (chop regime) for absorption to count
CVD_DIVERGENCE_DECAY_SECS = 300    # divergence label persists 5 minutes after detection then auto-clears


def _bucket_gap(gap_pct: float) -> str:
    if gap_pct >= GAP_LARGE_PCT:
        return "LARGE_GAP_UP"
    if gap_pct >= GAP_SMALL_PCT:
        return "SMALL_GAP_UP"
    if gap_pct <= -GAP_LARGE_PCT:
        return "LARGE_GAP_DN"
    if gap_pct <= -GAP_SMALL_PCT:
        return "SMALL_GAP_DN"
    return "FLAT"


def classify_and_accumulate_aggressor(
    state: "SymbolState",
    ltp: float,
    vtt: int,
    best_bid: float | None,
    best_ask: float | None,
    tick_ist: datetime,
) -> None:
    """Lee-Ready aggressor classifier on snapshot data.

    Tick rule (price-up -> BUY, price-down -> SELL) handles the majority of cases.
    Midpoint rule refines the zero-tick case: if LTP > midpoint the last trade
    was nearer the ask -> BUY aggressor; below -> SELL.

    Side effects: accumulate state.cvd and state.minute_buy_vol / minute_sell_vol.
    Roll the minute bar when tick_ist crosses a minute boundary.
    """
    # First tick of session — seed baselines, classify nothing yet
    if state._prev_vtt_for_aggressor == 0:
        state._prev_vtt_for_aggressor = vtt
        state._prev_ltp_for_aggressor = ltp
        state.current_minute_epoch = int(tick_ist.timestamp() // 60)
        return

    vol_delta = vtt - state._prev_vtt_for_aggressor
    if vol_delta <= 0:
        # No new volume since last tick; just update LTP reference (price may have
        # moved on a quote-only update with no print).
        state._prev_ltp_for_aggressor = ltp
        state._prev_vtt_for_aggressor = vtt
        return

    prev_ltp = state._prev_ltp_for_aggressor
    if ltp > prev_ltp:
        aggressor = "BUY"
    elif ltp < prev_ltp:
        aggressor = "SELL"
    else:
        # Zero-tick — try midpoint refinement first, fall back to last classification
        if best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2.0
            if ltp > midpoint:
                aggressor = "BUY"
            elif ltp < midpoint:
                aggressor = "SELL"
            else:
                aggressor = state._last_aggressor or "BUY"
        else:
            aggressor = state._last_aggressor or "BUY"

    state._last_aggressor = aggressor
    if aggressor == "BUY":
        state.cvd += vol_delta
        state.minute_buy_vol += vol_delta
    else:
        state.cvd -= vol_delta
        state.minute_sell_vol += vol_delta

    # Roll the minute bar if we crossed a minute boundary
    tick_minute = int(tick_ist.timestamp() // 60)
    if state.current_minute_epoch != tick_minute and state.current_minute_epoch != -1:
        state.last_completed_minute_buy = state.minute_buy_vol
        state.last_completed_minute_sell = state.minute_sell_vol
        state.last_completed_minute_delta = state.minute_buy_vol - state.minute_sell_vol
        state.recent_minute_deltas.append(state.last_completed_minute_delta)
        state.minute_buy_vol = 0
        state.minute_sell_vol = 0
    state.current_minute_epoch = tick_minute

    state._prev_ltp_for_aggressor = ltp
    state._prev_vtt_for_aggressor = vtt


def detect_flow_divergences(state: "SymbolState", tick_ist: datetime) -> None:
    """Update state.divergence_label based on CVD-vs-price structural relationships.

    Five patterns; first match wins. Labels auto-decay after CVD_DIVERGENCE_DECAY_SECS.

      BUYER_EXHAUSTION  : price makes new session high but CVD is lower than at prior high
      SELLER_EXHAUSTION : price makes new session low but CVD is higher than at prior low
      BREAKOUT_CONFIRMED: price > OR high AND last-completed-minute Δ-CVD strongly positive
      SELL_ABSORPTION   : flat regime (low ER) but 5-min Σ-Δ-CVD strongly negative
      BUY_ABSORPTION    : flat regime but 5-min Σ-Δ-CVD strongly positive
    """
    now = tick_ist.timestamp()
    ltp = state.ltp

    # Auto-decay an old label so it doesn't haunt the verdict forever
    if state.divergence_label and (now - state.divergence_ts) > CVD_DIVERGENCE_DECAY_SECS:
        state.divergence_label = ""

    if ltp <= 0:
        return

    # Pullback observer: flip the pullback-seen flags once price has moved materially
    # away from the prior swing extreme. Uses 0.5 * ATR when available; falls back to
    # 0.05% of price (~12 NIFTY pts) during the early-session warmup before ATR is ready.
    if state.atr > 0:
        pullback_dist = state.atr * 0.5
    else:
        pullback_dist = max(ltp * 0.0005, 1.0)
    if state.session_high_tracked > 0 and ltp < state.session_high_tracked - pullback_dist:
        state.high_pullback_seen = True
    if state.session_low_tracked > 0 and ltp > state.session_low_tracked + pullback_dist:
        state.low_pullback_seen = True

    # 1) Session-high tracking -> BUYER_EXHAUSTION on the new-high break
    # Requires a real pullback between the prior high and this break, otherwise
    # the first tick past the open (on a gap day) would false-fire.
    if state.session_high_tracked == 0 or ltp > state.session_high_tracked:
        if state.session_high_tracked > 0 and state.high_pullback_seen:
            cvd_delta_vs_old_high = state.cvd - state.cvd_at_session_high
            if cvd_delta_vs_old_high < -CVD_DIVERGENCE_DELTA_MIN:
                state.divergence_label = "BUYER_EXHAUSTION"
                state.divergence_ts = now
        state.session_high_tracked = ltp
        state.cvd_at_session_high = state.cvd
        state.high_pullback_seen = False  # require a fresh pullback before next exhaustion

    # 2) Session-low tracking -> SELLER_EXHAUSTION on the new-low break (symmetric)
    if state.session_low_tracked == 0 or ltp < state.session_low_tracked:
        if state.session_low_tracked > 0 and state.low_pullback_seen:
            cvd_delta_vs_old_low = state.cvd - state.cvd_at_session_low
            if cvd_delta_vs_old_low > CVD_DIVERGENCE_DELTA_MIN:
                state.divergence_label = "SELLER_EXHAUSTION"
                state.divergence_ts = now
        state.session_low_tracked = ltp
        state.cvd_at_session_low = state.cvd
        state.low_pullback_seen = False

    # 3) Opening-range breakout with flow confirmation
    if state.or_high is not None and state.or_finalized and ltp > state.or_high:
        if state.last_completed_minute_delta > CVD_BREAKOUT_MIN_DELTA:
            state.divergence_label = "BREAKOUT_CONFIRMED"
            state.divergence_ts = now
    elif state.or_low is not None and state.or_finalized and ltp < state.or_low:
        if state.last_completed_minute_delta < -CVD_BREAKOUT_MIN_DELTA:
            state.divergence_label = "BREAKOUT_CONFIRMED"
            state.divergence_ts = now

    # 4-5) Absorption — flat regime with strong directional flow
    if len(state.recent_minute_deltas) >= 5 and state.efficiency_ratio < CVD_ABSORPTION_ER_MAX:
        last5 = list(state.recent_minute_deltas)[-5:]
        sum5 = sum(last5)
        if sum5 < -CVD_ABSORPTION_5MIN_MIN:
            state.divergence_label = "SELL_ABSORPTION"
            state.divergence_ts = now
        elif sum5 > CVD_ABSORPTION_5MIN_MIN:
            state.divergence_label = "BUY_ABSORPTION"
            state.divergence_ts = now
