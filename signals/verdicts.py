"""Plain-English verdict composer.

Translates the multi-factor radar state (Z, ER, Hurst, OI flow, CVD divergence,
amber pre-alert, book imbalance) into a single decision-grade label + reason +
confidence tier. Display-only — does NOT mutate state, does NOT alter the
sig_state machine. The trader keeps the final call; this just compresses
the noise so they don't have to read every stat live.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from GammaLeak import SymbolState


# Threshold constants live in GammaLeak.py — these are imported lazily
# inside the function so that test scaffolds (which sometimes tweak them) see
# the live values, and so that circular-import ordering is irrelevant.

def compute_english_guidance(state: "SymbolState") -> tuple[str, str, str]:
    """Return (verdict, why, confidence) for a single instrument's current state.

    The decision branches are intentionally few — each tagged with the dominant
    driver so the trader can disagree with the radar when they have a reason
    the radar can't see (news, gut, position context).
    """
    from GammaLeak import (
        SIGNAL_WARMING_UP,
        ER_TREND_THRESHOLD,
        HURST_THRESHOLD,
    )

    # 0) No data / pre-warmup gates short-circuit everything else
    if state.ltp <= 0:
        return ("NO DATA", "feed has not delivered a tick", "LOW")
    if state.action_signal == SIGNAL_WARMING_UP:
        return ("WARMING UP", "stats need ~5 min of ticks", "LOW")

    z = state.z_score
    az = abs(z)
    er = state.efficiency_ratio
    hu = state.hurst
    trending = (er >= ER_TREND_THRESHOLD) or (hu >= HURST_THRESHOLD)
    choppy = (er < 0.4) and (hu < 0.50)

    # 0b) Flow divergences — loudest signals, take precedence over the rest
    dl = state.divergence_label
    if dl == "BUYER_EXHAUSTION":
        return ("BUYER EXHAUSTION",
                "new high but CVD weaker than prior high — fade the rip",
                "HIGH")
    if dl == "SELLER_EXHAUSTION":
        return ("SELLER EXHAUSTION",
                "new low but CVD recovering — fade the flush",
                "HIGH")
    if dl == "BREAKOUT_CONFIRMED":
        return ("BREAKOUT — TRUE FLOW",
                "OR break with aggressive flow alignment — go with it",
                "HIGH")
    if dl == "SELL_ABSORPTION":
        return ("SELL ABSORPTION",
                "price flat but heavy selling absorbed — downside trapped, bias long",
                "HIGH")
    if dl == "BUY_ABSORPTION":
        return ("BUY ABSORPTION",
                "price flat but heavy buying absorbed — upside trapped, bias short",
                "HIGH")

    # 1) Squeeze / blow-off context overrides — these are loud
    if state.gamma_flush_active and state.gamma_flush_side != 0:
        side = "long" if state.gamma_flush_side > 0 else "short"
        return ("GAMMA FLUSH",
                f"dealers covering {side} — expect snap reversal",
                "HIGH")

    flow = state.oi_flow_label or ""
    if flow == "SHORT COVER" and z < -1.5:
        return ("SQUEEZE RISK",
                "shorts being covered, don't chase fresh shorts",
                "MED")
    if flow == "LONG EXIT" and z > 1.5:
        return ("LONGS BAILING",
                "longs unwinding into strength, don't chase fresh longs",
                "MED")

    # 2) Confirmed setup (sig_state == 2): act, but qualify by regime
    if state.sig_state == 2:
        if trending:
            return ("GO WITH FLOW",
                    "confirmed signal in a trending regime — don't fade",
                    "MED")
        if choppy:
            direction = "SHORT" if z > 0 else "LONG"
            why = "confirmed reversal in chop"
            if flow == "NEW SHORTS" and z > 0:
                why = "new shorts entering on the highs — fade with them"
            elif flow == "NEW LONGS" and z < 0:
                why = "new longs entering on the lows — fade with them"
            return (f"FADE THE EXTREME ({direction})", why, "HIGH")
        return ("TAKE IT",
                "confirmed signal — regime ambiguous, smaller size",
                "MED")

    # 3) Alert state (sig_state == 1): wait for confirmation
    if state.sig_state == 1:
        side = "LONG" if state.alert_side > 0 else "SHORT"
        return (f"WAIT — building {side}",
                "z stretched but no re-cross yet, no entry",
                "LOW")

    # 4) Amber pre-alert (early warning, no signal yet)
    if state.amber_active:
        arrow = "↑" if state.amber_side > 0 else "↓"
        return (f"WATCH {arrow}",
                f"early-warning ({state.amber_reason or 'velocity'})",
                "LOW")

    # 5) Strong but flat: trending without z-extreme — let it ride
    if trending and az < 1.5:
        return ("TREND OK",
                "ER/Hurst trending — don't fade, wait for pullback",
                "LOW")

    # 6) Book-imbalance edge (futures only; spot indices have book_imbalance=0)
    if abs(state.book_imbalance) >= 0.20 and az < 1.5:
        bias = "buyers" if state.book_imbalance > 0 else "sellers"
        return ("BOOK PRESSURE",
                f"{bias} stacked in the depth — leans that way",
                "LOW")

    # 7) Default: no edge here
    return ("STAND ASIDE",
            "no setup — z near mean, regime mixed",
            "LOW")
