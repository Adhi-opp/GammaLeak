"""OI Delta flow classification + timeline recorder.

`classify_oi_flow` runs every tick that touches the NIFTY spot — it diffs the
current ATM±2 strikes' CE/PE OI against the prior PCR snapshot, cross-references
with price direction, and labels the resulting quadrant (NEW LONGS / NEW SHORTS /
SHORT COVER / LONG EXIT / NEUTRAL) plus the CE-vs-PE divergence flavor.

`record_oi_flow_sample` downsamples those classifications + raw deltas into a
30-min ring buffer keyed by timestamp, so the anchored velocity chart on the
web dashboard has continuous time-series to plot.

The module-level `_oi_flow_last_sample_ts` is rebound by record_oi_flow_sample
via `global`; this is why it lives here and not in core/state.py (rebinds
across modules don't propagate).
"""
from __future__ import annotations

from core.config import OI_FLOW_MIN_DELTA, OI_FLOW_TIMELINE_SAMPLE_SECS
from core.models import SymbolState
from core.state import (
    DISPLAY_NAMES,
    symbol_states,
    pcr_state,
    oi_levels_state,
    oi_flow_timeline,
)
from gammaleak_runtime.io_logs import append_oi_state_row


_oi_flow_last_sample_ts: float = 0.0


def record_oi_flow_sample(nifty_state: SymbolState, timestamp: float) -> None:
    # Downsamples per-tick classify_oi_flow output into one row every ~5s.
    # Each sample carries: spot, classifications, raw deltas (for the secondary
    # axis), and the current OI anchors (top CE/PE walls + max-pain — for the
    # primary axis dashed reference lines). Anchors are read from
    # oi_levels_state which refresh_oi_levels updates on its own cadence.
    global _oi_flow_last_sample_ts
    if timestamp - _oi_flow_last_sample_ts < OI_FLOW_TIMELINE_SAMPLE_SECS:
        return
    _oi_flow_last_sample_ts = timestamp
    if nifty_state.ltp <= 0:
        return
    lvl = oi_levels_state.levels.get("NIFTY")
    top_ce_strike = lvl.ce_walls[0].strike if lvl and lvl.ce_walls else 0
    top_pe_strike = lvl.pe_walls[0].strike if lvl and lvl.pe_walls else 0
    max_pain_strike = lvl.max_pain if lvl else 0
    # NIFTY_FUT overlay — front-month futures plotted against spot on the
    # primary axis surfaces basis divergence (futures leading spot during
    # capitulation hedging). Resolved each sample because the FUT key changes
    # on monthly expiry rollover.
    fut_ltp = 0.0
    for k, name in DISPLAY_NAMES.items():
        if name == "NIFTY_FUT":
            fs = symbol_states.get(k)
            if fs is not None and fs.ltp > 0:
                fut_ltp = fs.ltp
            break
    oi_flow_timeline.append((
        timestamp,
        nifty_state.ltp,
        nifty_state.oi_flow_label or "",
        nifty_state.oi_flow_ce_pe or "",
        nifty_state.oi_flow_ce_delta,
        nifty_state.oi_flow_pe_delta,
        max_pain_strike,
        top_ce_strike,
        top_pe_strike,
        fut_ltp,
    ))
    # Mirror to logs/<date>_oi_state.csv on the same 5s cadence so the chart's
    # walls + delta velocities are retroactively inspectable. try/except keeps
    # any disk error from ever propagating into the live engine path.
    try:
        append_oi_state_row(
            timestamp=timestamp,
            spot=nifty_state.ltp, fut=fut_ltp,
            top_ce_strike=top_ce_strike, top_pe_strike=top_pe_strike,
            max_pain=max_pain_strike,
            ce_delta=nifty_state.oi_flow_ce_delta,
            pe_delta=nifty_state.oi_flow_pe_delta,
            oi_flow_label=nifty_state.oi_flow_label or "",
            oi_flow_ce_pe=nifty_state.oi_flow_ce_pe or "",
        )
    except Exception:
        pass


def classify_oi_flow(nifty_state: SymbolState) -> None:
    """V5.1: 4-quadrant OI Delta classification + CE/PE divergence.
    Uses the 5-minute OI snapshot delta cross-referenced with price direction."""
    if pcr_state.oi_snapshot_ts is None or pcr_state.ltp_at_oi_snapshot <= 0:
        return
    if nifty_state.ltp <= 0:
        return

    atm = int(round(nifty_state.ltp / 50.0) * 50)
    window = [atm - 100, atm - 50, atm, atm + 50, atm + 100]

    # --- OI deltas across ATM window ---
    ce_now = sum(pcr_state.ce_oi.get(s, 0) for s in window)
    ce_prev = sum(pcr_state.prev_ce_oi.get(s, 0) for s in window)
    pe_now = sum(pcr_state.pe_oi.get(s, 0) for s in window)
    pe_prev = sum(pcr_state.prev_pe_oi.get(s, 0) for s in window)

    ce_delta = ce_now - ce_prev
    pe_delta = pe_now - pe_prev
    total_oi_delta = ce_delta + pe_delta
    # Persist raw deltas so timeline samples can chart Flow Velocity.
    nifty_state.oi_flow_ce_delta = int(ce_delta)
    nifty_state.oi_flow_pe_delta = int(pe_delta)

    # --- Price direction since last OI snapshot ---
    price_delta = nifty_state.ltp - pcr_state.ltp_at_oi_snapshot

    # --- 4-Quadrant Classification ---
    if abs(total_oi_delta) < OI_FLOW_MIN_DELTA:
        nifty_state.oi_flow_label = "NEUTRAL"
    elif total_oi_delta > 0 and price_delta > 0:
        nifty_state.oi_flow_label = "NEW LONGS"
    elif total_oi_delta > 0 and price_delta < 0:
        nifty_state.oi_flow_label = "NEW SHORTS"
    elif total_oi_delta < 0 and price_delta > 0:
        nifty_state.oi_flow_label = "SHORT COVER"
    elif total_oi_delta < 0 and price_delta < 0:
        nifty_state.oi_flow_label = "LONG EXIT"
    else:
        nifty_state.oi_flow_label = "NEUTRAL"

    # --- CE/PE Divergence ---
    ce_sig = abs(ce_delta) >= OI_FLOW_MIN_DELTA
    pe_sig = abs(pe_delta) >= OI_FLOW_MIN_DELTA

    if ce_sig and ce_delta > 0 and (not pe_sig or pe_delta <= 0):
        nifty_state.oi_flow_ce_pe = "CE WRITERS IN"    # Bearish near-term
    elif pe_sig and pe_delta > 0 and (not ce_sig or ce_delta <= 0):
        nifty_state.oi_flow_ce_pe = "PE WRITERS IN"    # Bullish near-term
    elif ce_sig and pe_sig and ce_delta > 0 and pe_delta > 0:
        nifty_state.oi_flow_ce_pe = "STRADDLE BUILD"   # Range-bound
    elif ce_sig and pe_sig and ce_delta < 0 and pe_delta < 0:
        nifty_state.oi_flow_ce_pe = "UNWINDING"         # Directional move imminent
    else:
        nifty_state.oi_flow_ce_pe = "NEUTRAL"
