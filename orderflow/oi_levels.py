"""OI Levels — max pain + gamma walls derived from the live PCR snapshot.

`refresh_oi_levels` runs on the OI_LEVELS_REFRESH_SECS cadence (engine
orchestrator calls it), computes max-pain across the active PCR strike window,
and picks the top CE walls (resistance, above spot) and PE walls (support,
below spot) within the band. Stores the result in oi_levels_state.levels[symbol]
so the dashboard's anchored velocity chart can read the latest anchors.

`_compute_max_pain` and `_find_gamma_walls` are the pure-math helpers.
"""
from __future__ import annotations

from core.config import (
    OI_LEVELS_REFRESH_SECS,
    OI_LEVELS_BAND_PCT,
    OI_LEVELS_WALLS_COUNT,
    OI_LEVELS_MIN_STRIKES,
    OI_LEVELS_STALE_SECS,
)
from core.models import OILevels, OIWall
from core.state import oi_levels_state, symbol_states, pcr_state


def _compute_max_pain(ce_oi: dict[int, float], pe_oi: dict[int, float]) -> tuple[int, float]:
    """Return (max_pain_strike, min_pain_value).

    pain(P) = Σ max(0, P − K)·OI_CE(K)  +  Σ max(0, K − P)·OI_PE(K)

    Writers of CE at K lose (P−K) per contract if P > K; writers of PE at K
    lose (K−P) if K > P. Max-pain = strike P* that minimizes total writer
    pain. Only candidate P's are listed strikes (pain is piecewise-linear
    with kinks at strikes, so the minimum is always at a strike).
    """
    candidates = sorted(set(ce_oi.keys()) | set(pe_oi.keys()))
    if not candidates:
        return 0, 0.0
    best_strike = candidates[0]
    best_pain = float("inf")
    for P in candidates:
        pain = 0.0
        for K, oi in ce_oi.items():
            if P > K and oi > 0:
                pain += (P - K) * oi
        for K, oi in pe_oi.items():
            if K > P and oi > 0:
                pain += (K - P) * oi
        if pain < best_pain:
            best_pain = pain
            best_strike = P
    return best_strike, best_pain


def _find_gamma_walls(
    oi_by_strike: dict[int, float],
    spot: float,
    direction: str,              # "above" → CE/resistance, "below" → PE/support
    band_pct: float,
    count: int,
) -> list[OIWall]:
    """Top `count` strikes by OI in `direction` from spot, within ±band_pct.

    Filters zero-OI strikes. Returns sorted descending by OI.
    """
    if spot <= 0 or not oi_by_strike:
        return []
    lo = spot * (1.0 - band_pct)
    hi = spot * (1.0 + band_pct)
    picks: list[OIWall] = []
    for strike, oi in oi_by_strike.items():
        if oi <= 0:
            continue
        if direction == "above" and not (spot <= strike <= hi):
            continue
        if direction == "below" and not (lo <= strike <= spot):
            continue
        picks.append(OIWall(
            strike=int(strike),
            oi=float(oi),
            dist_pct=((strike - spot) / spot) * 100.0,
        ))
    picks.sort(key=lambda w: w.oi, reverse=True)
    return picks[:count]


def refresh_oi_levels(now: float) -> None:
    """Recompute NIFTY OI levels. Gated by OI_LEVELS_REFRESH_SECS.

    PCR_EXPIRY_CODE is looked up lazily because it lives in GammaLeak.py
    and is REBOUND by the bootloader (`PCR_EXPIRY_CODE = nifty_expiry`). A
    rebind in another module wouldn't propagate to a snapshot held in this
    one, so we re-import at each call to always see the current value.
    """
    if now - oi_levels_state.last_refresh_ts < OI_LEVELS_REFRESH_SECS:
        return

    from GammaLeak import PCR_EXPIRY_CODE  # deferred — see docstring

    nifty_state = symbol_states.get("NSE_INDEX|Nifty 50")
    spot = float(nifty_state.ltp) if nifty_state else 0.0

    lvl = OILevels(symbol="NIFTY", spot=spot, expiry=PCR_EXPIRY_CODE or "")

    # Snapshot to avoid mutation mid-compute (WebSocket callback may be firing)
    ce_oi = dict(pcr_state.ce_oi)
    pe_oi = dict(pcr_state.pe_oi)
    active_strikes = {s for s in set(ce_oi) | set(pe_oi)
                      if ce_oi.get(s, 0) > 0 or pe_oi.get(s, 0) > 0}
    lvl.n_strikes = len(active_strikes)

    if spot <= 0:
        lvl.stale_reason = "no spot"
    elif lvl.n_strikes < OI_LEVELS_MIN_STRIKES:
        lvl.stale_reason = f"only {lvl.n_strikes} strikes"
    elif pcr_state.last_updated is None:
        lvl.stale_reason = "no OI update yet"
    elif now - pcr_state.last_updated > OI_LEVELS_STALE_SECS:
        lvl.stale_reason = f"OI {int(now - pcr_state.last_updated)}s old"
    else:
        lvl.stale = False
        mp, _ = _compute_max_pain(ce_oi, pe_oi)
        lvl.max_pain = int(mp)
        lvl.max_pain_dist_pct = ((mp - spot) / spot) * 100.0 if spot else 0.0
        lvl.ce_walls = _find_gamma_walls(ce_oi, spot, "above", OI_LEVELS_BAND_PCT, OI_LEVELS_WALLS_COUNT)
        lvl.pe_walls = _find_gamma_walls(pe_oi, spot, "below", OI_LEVELS_BAND_PCT, OI_LEVELS_WALLS_COUNT)

    oi_levels_state.levels["NIFTY"] = lvl
    oi_levels_state.last_refresh_ts = now
