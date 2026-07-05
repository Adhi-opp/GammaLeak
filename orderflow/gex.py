"""Net dealer gamma exposure (GEX) over the subscribed ATM strike window.

Research-track P2 (shadow — nothing gates on this yet). Hypothesis: when the
dealer complex is net LONG gamma their hedging dampens moves (fades work);
net SHORT gamma, hedging amplifies (fades are toxic). OI and per-strike gamma
are both already on the feed; this module just multiplies them.

Sign model (the one assumption, to be VALIDATED by fit before anything trusts
the sign): the classic SPX convention — dealers are long the call-side gamma
book and short the put-side book, so

    net_gex_1pct = (Σ γ(K)·OI_CE(K) − Σ γ(K)·OI_PE(K)) · spot² · 0.01

γ(K) is the strike's latest feed gamma (BS gamma is put/call-identical at a
strike, so the mixed CE/PE history deque is usable as-is), staleness-bounded
by GEX_STALE_SECS. Units are feed-native gamma × contracts × index-points² —
internally consistent, which is all the sign/tercile research needs.

Limitations (deliberate, v1): window-local (subscribed ATM±window only, wings
excluded), front-expiry only, and the sign convention is unproven for Indian
weeklies where retail net-buys both wings. The per-strike snapshot log exists
precisely so the sign model can be re-derived offline without re-collecting.

`update_gex` follows the oi_flow module pattern: called on every NIFTY tick
from classify_dynamic_regime, throttles itself via a module-level timestamp.
"""
from __future__ import annotations

from collections import deque

from core.config import GEX_SNAPSHOT_SECS, GEX_STALE_SECS, GEX_MIN_STRIKES
from core.models import SymbolState
from core.state import pcr_state
from gammaleak_runtime.io_logs import append_gex_snapshot


_last_gex_ts: float = 0.0


def _latest_gamma(history: deque | None, now: float) -> float | None:
    """Most recent positive gamma within GEX_STALE_SECS, else None.

    Walks backwards so a single zero tick (quote update without greeks)
    doesn't blank a strike that had a valid reading moments ago — same
    pattern as vol_surface._latest_iv.
    """
    if not history:
        return None
    for ts, g in reversed(history):
        if (now - ts) > GEX_STALE_SECS:
            return None
        if g > 0.0:
            return float(g)
    return None


def compute_gex(spot: float, now: float) -> tuple[float, float, float, list[tuple[int, float, float, float, float, float]]] | None:
    """Return (gex_ce, gex_pe, net_gex_1pct, per_strike_rows) or None if the
    chain hasn't delivered enough live greeks (< GEX_MIN_STRIKES strikes).
    Row = (strike, gamma, ce_oi, pe_oi, ce_delta, pe_delta) — deltas ride
    along for the charm proxy (B4); 0.0 when the feed hasn't priced them."""
    if spot <= 0:
        return None
    gex_ce = 0.0
    gex_pe = 0.0
    rows: list[tuple[int, float, float, float, float, float]] = []
    for strike in sorted(set(pcr_state.ce_oi) | set(pcr_state.pe_oi)):
        g = _latest_gamma(pcr_state.gamma_history.get(strike), now)
        if g is None:
            continue
        ce = float(pcr_state.ce_oi.get(strike, 0.0))
        pe = float(pcr_state.pe_oi.get(strike, 0.0))
        if ce <= 0.0 and pe <= 0.0:
            continue
        gex_ce += g * ce
        gex_pe += g * pe
        rows.append((strike, g, ce, pe,
                     float(pcr_state.delta_by_strike_ce.get(strike, 0.0)),
                     float(pcr_state.delta_by_strike_pe.get(strike, 0.0))))
    if len(rows) < GEX_MIN_STRIKES:
        return None
    net_1pct = (gex_ce - gex_pe) * spot * spot * 0.01
    return gex_ce, gex_pe, net_1pct, rows


def update_gex(nifty_state: SymbolState, timestamp: float) -> None:
    """Throttled per-minute GEX refresh + snapshot append. Mutates
    nifty_state.gex_ce / gex_pe / gex_net_1pct; leaves them untouched (stale
    values persist) when the chain can't currently support a read."""
    global _last_gex_ts
    if timestamp - _last_gex_ts < GEX_SNAPSHOT_SECS:
        return
    _last_gex_ts = timestamp

    out = compute_gex(nifty_state.ltp, timestamp)
    if out is None:
        return
    gex_ce, gex_pe, net_1pct, rows = out
    nifty_state.gex_ce = gex_ce
    nifty_state.gex_pe = gex_pe
    nifty_state.gex_net_1pct = net_1pct
    # Disk errors must never propagate into the tick path (io_logs contract).
    try:
        append_gex_snapshot(timestamp, nifty_state.ltp, rows, net_1pct)
    except Exception:
        pass
