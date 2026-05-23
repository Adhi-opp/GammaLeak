"""Dashboard state serializers — engine state → JSON-safe dicts.

Every function here converts a slice of engine state into a shape the
frontend can render. The web_server's broadcast loop calls `build_state_payload`
4x/sec and ships the result as a single JSON blob over the WebSocket.

The module-level `_engine_ready` flag is the bootloader's hand-off: serializers
return empty/booting payloads until the engine flips it True at the end of
its startup sequence. The flag lives here (not in core/state) because only
this module's serializers care about it.
"""
from __future__ import annotations

import time
from datetime import datetime

# Engine module — serializers reach into its live state. This is a read-only
# coupling (nothing here mutates GammaLeak state); it's the boundary
# where engine internals get translated into wire format.
import GammaLeak as engine


_engine_ready = False


def serialize_symbol(key: str) -> dict:
    s = engine.symbol_states[key]
    display = engine.get_display_name(key)

    # Sonar catalyst annotation
    sonar_tag = ""
    sonar_ctx = engine._sonar_last_contexts.get(display)
    if sonar_ctx and sonar_ctx.has_catalyst:
        sonar_tag = sonar_ctx.catalyst_type

    return {
        "key": key,
        "display": display,
        "ltp": round(s.ltp, 2),
        "vwap": round(s.vwap, 2),
        "std_dev": round(s.std_dev, 4),
        "z_score": round(s.z_score, 4),
        "efficiency_ratio": round(s.efficiency_ratio, 2),
        "hurst": round(s.hurst, 2),
        "atr": round(s.atr, 1),
        "regime": s.regime,
        "dynamic_regime": s.dynamic_regime,
        "action_signal": s.action_signal,
        "action_style": s.action_style,
        "sig_state": s.sig_state,
        "ltp_style": s.ltp_style,
        "thesis_age_secs": round(s.thesis_age_secs, 0),
        "thesis_decay": s.thesis_decay,
        "regime_shift_alert": s.regime_shift_alert,
        "gamma_flush_active": s.gamma_flush_active,
        "gamma_flush_side": s.gamma_flush_side,
        "oi_roc_ce_atm": round(s.oi_roc_ce_atm, 1),
        "oi_roc_pe_atm": round(s.oi_roc_pe_atm, 1),
        "oi_flow_label": s.oi_flow_label,
        "oi_flow_ce_pe": s.oi_flow_ce_pe,
        "implied_upper": round(s.implied_upper, 0),
        "implied_lower": round(s.implied_lower, 0),
        "straddle_premium": round(s.straddle_premium, 0),
        "anchor_gate": s.anchor_gate,
        "sonar_tag": sonar_tag,
        "tps": round(s.tps, 0),
        # Institutional Phase 1
        "conviction_score": s.conviction_score,
        "setup_label": s.setup_label,
        "event_blackout_reason": s.event_blackout_reason,
        # V5.2 Micro-Structural Layer (display-only early-warning tier)
        "micro_vwap": round(s.micro_vwap, 2),
        "micro_std_dev": round(s.micro_std_dev, 4),
        "micro_z_score": round(s.micro_z_score, 4),
        "z_velocity": round(s.z_velocity, 4),
        "amber_active": s.amber_active,
        "amber_side": s.amber_side,
        "amber_reason": s.amber_reason,
        "tick_rate_short": round(s.tick_rate_short, 2),
        "tick_rate_baseline": round(s.tick_rate_baseline, 2),
        "tick_rate_spike": s.tick_rate_spike,
        # Order-flow (futures only — book_imbalance stays 0 for indices)
        "tbq": round(s.tbq, 0),
        "tsq": round(s.tsq, 0),
        "book_imbalance": round(s.book_imbalance, 3),
        # Phase 1 OFI — observational; rendered in the card math dropdown.
        "delta_ofi_smoothed": round(s.delta_ofi_smoothed, 0),
        "absorption_label": s.absorption_label,
        # Pre-open gap context
        "prior_close": round(s.prior_close, 2) if s.prior_close > 0 else None,
        "gap_pct": round(s.gap_pct, 3) if s.gap_bucket else None,
        "gap_bucket": s.gap_bucket or "",
        # Plain-English decision layer — spot indices mirror their FUT sibling
        # since spot has no flow data of its own.
        **(lambda v: {
            "english_verdict": v[0],
            "english_why": v[1],
            "english_confidence": v[2],
        })(engine.get_effective_verdict(key)),
        # Order-flow / CVD (Phase 1B)
        "cvd": int(s.cvd),
        "minute_buy_vol": int(s.minute_buy_vol),
        "minute_sell_vol": int(s.minute_sell_vol),
        "last_completed_minute_delta": int(s.last_completed_minute_delta),
        "divergence_label": s.divergence_label,
    }


def serialize_pcr() -> dict:
    nifty = engine.symbol_states.get("NSE_INDEX|Nifty 50")
    nifty_ltp = nifty.ltp if nifty else 0.0
    ratio, ce_sum, pe_sum, window_strikes, atm_strike = (
        engine.pcr_state.get_dynamic_snapshot(nifty_ltp)
        if nifty_ltp > 0
        else (None, 0.0, 0.0, (), None)
    )

    prev_ce = sum(engine.pcr_state.prev_ce_oi.get(s, 0) for s in window_strikes) if window_strikes else 0
    prev_pe = sum(engine.pcr_state.prev_pe_oi.get(s, 0) for s in window_strikes) if window_strikes else 0

    return {
        "ratio": round(ratio, 2) if ratio else None,
        "ce_total": round(ce_sum, 0),
        "pe_total": round(pe_sum, 0),
        "ce_delta": round(ce_sum - prev_ce, 0),
        "pe_delta": round(pe_sum - prev_pe, 0),
        "atm_strike": atm_strike,
        "strikes": list(window_strikes) if window_strikes else [],
    }


def serialize_macro() -> dict:
    result = {}
    bias = engine.compute_desk_bias()
    result["_bias"] = {
        "label": bias["label"],
        "score": round(float(bias["score"]), 2),
    }
    # VIX context from live WebSocket data
    vix_val, vix_regime, vix_text = engine.get_vix_state()
    result["VIX"] = {
        "value": round(vix_val, 2) if vix_val > 0 else None,
        "regime": vix_regime,
        "text": vix_text,
    }
    # USDINR compact readout (card was removed from main grid; surface it here)
    usdinr_key = next(iter(engine.USDINR_KEYS), None)
    if usdinr_key and usdinr_key in engine.symbol_states:
        st = engine.symbol_states[usdinr_key]
        change_pct = None
        if st.session_open and st.session_open > 0 and st.ltp > 0:
            change_pct = (st.ltp / st.session_open - 1.0) * 100.0
        result["USDINR"] = {
            "value": round(st.ltp, 4) if st.ltp > 0 else None,
            "session_open": round(st.session_open, 4) if st.session_open else None,
            "change_pct": round(change_pct, 3) if change_pct is not None else None,
            "text": bias.get("usdinr_text", ""),
        }
    else:
        result["USDINR"] = {"value": None, "text": "USDINR: awaiting feed"}
    return result


def serialize_fii() -> dict | None:
    snap = engine._fii_snapshot
    if not snap or not snap.fii:
        return None
    return {
        "date": str(snap.as_of_date),
        "fii_fut_net": snap.fii.fut_idx_net,
        "fii_opt_net": snap.fii.opt_idx_net,
        "fii_bias": snap.fii_bias,
        "dii_fut_net": snap.dii.fut_idx_net if snap.dii else 0,
        "dii_bias": snap.dii_bias,
        "fii_cash_buy": round(snap.fii_cash.buy_amount, 2) if snap.fii_cash else None,
        "fii_cash_sell": round(snap.fii_cash.sell_amount, 2) if snap.fii_cash else None,
        "fii_cash_net": round(snap.fii_cash.net, 2) if snap.fii_cash else None,
        "dii_cash_buy": round(snap.dii_cash.buy_amount, 2) if snap.dii_cash else None,
        "dii_cash_sell": round(snap.dii_cash.sell_amount, 2) if snap.dii_cash else None,
        "dii_cash_net": round(snap.dii_cash.net, 2) if snap.dii_cash else None,
        "summary": snap.format_summary(),
    }


def serialize_focus() -> list[dict]:
    """Replicate the focus panel ranking."""
    rows = []
    for key in engine.INSTRUMENT_KEYS[:engine.TOP_N_ROWS]:
        s = engine.symbol_states[key]
        if s.action_signal == engine.SIGNAL_TREND_STAND_DOWN:
            continue
        raw_focus_score = abs(s.z_score) * s.efficiency_ratio
        if s.efficiency_ratio < engine.ER_TREND_THRESHOLD or s.hurst < engine.HURST_THRESHOLD:
            score = 0.0
        else:
            score = raw_focus_score
        if s.regime_shift_alert or s.thesis_decay:
            rank = 3
        elif s.sig_state == 2:
            rank = 2
        elif s.sig_state == 1:
            rank = 1
        else:
            rank = 0
        if rank == 0 and abs(s.z_score) < 1.0:
            continue
        rows.append({
            "display": engine.get_display_name(key),
            "score": round(score, 2),
            "z_score": round(s.z_score, 4),
            "signal": s.action_signal.split(" | ")[0],
            "rank": rank,
        })
    rows.sort(key=lambda r: (-r["rank"], -r["score"]))
    return rows[:engine.FOCUS_TOP_N]


def serialize_drivers() -> list[dict]:
    """Institutional Phase 2: Index Driver Panel payload."""
    engine.refresh_index_driver_metrics(time.time())
    out = []
    for m in engine.index_driver_state.metrics:
        out.append({
            "pair_a": engine.get_display_name(m.pair[0]) if m.pair[0] else "",
            "pair_b": engine.get_display_name(m.pair[1]) if m.pair[1] else "",
            "corr": round(m.corr, 3),
            "lead_lag_secs": m.lead_lag_secs,
            "lead_corr": round(m.lead_corr, 3),
            "drag": m.drag,
            "drag_detail": m.drag_detail,
            "n_points": m.n_points,
            "stale": m.stale,
        })
    return out


def serialize_driver_amber() -> dict:
    """V5.2 Layer 4: cross-asset acceleration amber for NIFTY."""
    ds = engine.index_driver_state
    return {
        "nifty_active": ds.nifty_driver_amber,
        "nifty_side": ds.nifty_driver_amber_side,
        "nifty_source": ds.nifty_driver_amber_source,
        "nifty_velocity": round(ds.nifty_driver_amber_velocity, 4),
    }


def serialize_oi_levels() -> dict:
    """Institutional Phase 3: OI Levels (max-pain + gamma walls) payload."""
    engine.refresh_oi_levels(time.time())
    out: dict = {}
    for sym, lvl in engine.oi_levels_state.levels.items():
        out[sym] = {
            "symbol": lvl.symbol,
            "spot": round(lvl.spot, 2),
            "expiry": lvl.expiry,
            "max_pain": lvl.max_pain,
            "max_pain_dist_pct": round(lvl.max_pain_dist_pct, 3),
            "ce_walls": [
                {"strike": w.strike, "oi": w.oi, "dist_pct": round(w.dist_pct, 3)}
                for w in lvl.ce_walls
            ],
            "pe_walls": [
                {"strike": w.strike, "oi": w.oi, "dist_pct": round(w.dist_pct, 3)}
                for w in lvl.pe_walls
            ],
            "n_strikes": lvl.n_strikes,
            "stale": lvl.stale,
            "stale_reason": lvl.stale_reason,
        }
    return out


def serialize_sonar() -> list[dict]:
    result = []
    for ctx in engine._sonar_last_contexts.values():
        if ctx.has_catalyst:
            result.append({
                "instrument": ctx.instrument,
                "catalyst_type": ctx.catalyst_type,
                "summary": ctx.summary[:120],
                "confidence": ctx.confidence,
            })
    return result


def serialize_pre_open() -> list[dict]:
    """Pre-open gap snapshot for indices + index futures."""
    out: list[dict] = []
    targets = []
    # Spot indices always
    for k in ("NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"):
        if k in engine.symbol_states:
            targets.append(k)
    # Index futures (dynamically named by bootloader)
    for k, name in engine.DISPLAY_NAMES.items():
        if name in ("NIFTY_FUT", "BN_FUT") and k in engine.symbol_states:
            targets.append(k)

    for k in targets:
        s = engine.symbol_states[k]
        out.append({
            "display": engine.get_display_name(k),
            "prior_close": round(s.prior_close, 2) if s.prior_close > 0 else None,
            "open_or_ltp": round(s.ltp, 2) if s.ltp > 0 else None,
            "gap_pct": round(s.gap_pct, 3) if s.gap_bucket else None,
            "gap_bucket": s.gap_bucket or "",
        })
    return out


def serialize_global_indices() -> dict:
    from analytics.global_indices import get_snapshot
    return get_snapshot().to_payload()


def serialize_oi_flow_timeline() -> dict:
    """Sliding-window OI flow history for the anchored dual-axis chart.

    Per sample: spot + classifications + raw ce/pe deltas (for Flow Velocity
    lines on the right axis) + max-pain/top-walls (for dashed anchor lines on
    the left axis). The dashed lines redraw every render from the latest
    sample's anchor values, so they slide as OI walls shift through the day.
    """
    if not _engine_ready:
        return {"samples": [], "ts_now": time.time(), "window_secs": 0}
    samples = list(engine.oi_flow_timeline)
    out_samples = []
    for s in samples:
        # Backwards-compat: older samples from earlier shapes still flow
        # through if the deque hasn't rotated yet (4-tuple strip-chart era,
        # 9-tuple pre-FUT-overlay era, 10-tuple current).
        fut = 0.0
        if len(s) >= 10:
            ts, spot, label, ce_pe, ce_d, pe_d, mp, top_ce, top_pe, fut = s[:10]
        elif len(s) >= 9:
            ts, spot, label, ce_pe, ce_d, pe_d, mp, top_ce, top_pe = s[:9]
        else:
            ts, spot, label, ce_pe = s[:4]
            ce_d = pe_d = mp = top_ce = top_pe = 0
        out_samples.append({
            "ts": ts,
            "spot": round(spot, 2),
            "fut": round(fut, 2),
            "label": label,
            "ce_pe": ce_pe,
            "ce_delta": int(ce_d),
            "pe_delta": int(pe_d),
            "max_pain": int(mp),
            "top_ce_strike": int(top_ce),
            "top_pe_strike": int(top_pe),
        })
    return {
        "samples": out_samples,
        "ts_now": time.time(),
        "window_secs": engine.OI_FLOW_TIMELINE_WINDOW_SECS,
    }


async def _resolve_oi_chain_params() -> tuple[str, str, str] | None:
    """Resolve (underlying_key, expiry_YYYY-MM-DD, date_YYYY-MM-DD) for /market/oi.

    Walks the engine's instrument master to convert the active option expiry
    code (e.g. '26MAY') into the YYYY-MM-DD form Upstox needs. Returns None
    until the engine has booted and resolved its current PCR keys.
    """
    if not _engine_ready:
        return None
    if not engine._instrument_master_by_symbol:
        return None
    expiry_iso: str | None = None
    # Any PCR key resolves to a specific option contract whose `expiry` is the date we want.
    sample_ce = next(iter(engine.PCR_KEYS.get("CE", [])), None) if engine.PCR_KEYS else None
    if sample_ce:
        for instruments in engine._instrument_master_by_symbol.values():
            match = next((m for m in instruments if m.get("instrument_key") == sample_ce), None)
            if match and match.get("expiry"):
                expiry_iso = match["expiry"]
                break
    if not expiry_iso:
        # Fallback: ask engine to re-resolve from master directly.
        key, _ = await engine.get_active_expiry_key("NIFTY", "OPT")
        if key:
            for instruments in engine._instrument_master_by_symbol.values():
                match = next((m for m in instruments if m.get("instrument_key") == key), None)
                if match and match.get("expiry"):
                    expiry_iso = match["expiry"]
                    break
    if not expiry_iso:
        return None
    today_iso = datetime.now(engine.IST).date().isoformat()
    return ("NSE_INDEX|Nifty 50", expiry_iso, today_iso)


def serialize_oi_chain() -> dict:
    """Full-chain OI snapshot with dynamic ATM-relative windowing.

    Each render recomputes the ATM window from the LIVE spot (engine.symbol_states),
    so as price moves intraday the near-ATM walls follow without the poller
    having to re-fetch. Max Pain stays on full chain (independent of spot).
    """
    from orderflow.oi_chain import get_snapshot, compute_max_pain, near_atm_walls, deep_clusters
    snap = get_snapshot()
    if snap.chain is None:
        return {"available": False, "last_error": snap.last_error, "last_refresh_ts": 0}
    # Live spot from engine state — NOT the closing price the API returns.
    nifty_state = engine.symbol_states.get("NSE_INDEX|Nifty 50")
    live_spot = nifty_state.ltp if nifty_state and nifty_state.ltp > 0 else snap.chain.spot_closing_price
    mp_strike, _ = compute_max_pain(snap.chain.strikes)
    walls = near_atm_walls(snap.chain.strikes, live_spot, n_strikes_each_side=5)
    clusters = deep_clusters(snap.chain.strikes, live_spot, excluded_window=5, top_n=3)
    return {
        "available": True,
        "expiry": snap.chain.expiry,
        "n_strikes": snap.chain.n_strikes,
        "total_calls": snap.chain.total_calls,
        "total_puts": snap.chain.total_puts,
        "pcr_full_chain": round(snap.chain.pcr, 3),
        "max_pain_full_chain": mp_strike,
        "max_pain_dist_pct": round((mp_strike - live_spot) / live_spot * 100.0, 3) if live_spot else 0.0,
        "live_spot": round(live_spot, 2),
        "near_atm": walls,
        "deep_clusters": clusters,
        "last_refresh_ts": snap.last_refresh_ts,
        "last_error": snap.last_error,
    }


def build_state_payload() -> dict:
    """Master serialization — full dashboard state as JSON."""
    if not _engine_ready or not engine.symbol_states:
        return {"ts": time.time(), "session": "BOOTING", "symbols": [],
                "pcr": {}, "macro": {}, "fii": None, "focus": [], "sonar": [],
                "drivers": [], "driver_amber": {}, "oi_levels": {}, "pre_open": [],
                "global_indices": {"regions": {}, "last_refresh_ts": 0, "last_error": ""}}

    now_ist = datetime.now(engine.IST)
    session = engine.get_session_badge(now_ist)

    symbols = []
    visible_keys = [k for k in engine.INSTRUMENT_KEYS if k not in engine.HIDDEN_FROM_CARDS]
    for k in visible_keys[:engine.TOP_N_ROWS]:
        if k in engine.symbol_states:
            symbols.append(serialize_symbol(k))

    return {
        "ts": time.time(),
        "session": session,
        "symbols": symbols,
        "pcr": serialize_pcr(),
        "macro": serialize_macro(),
        "fii": serialize_fii(),
        "focus": serialize_focus(),
        "sonar": serialize_sonar(),
        "drivers": serialize_drivers(),
        "driver_amber": serialize_driver_amber(),
        "oi_levels": serialize_oi_levels(),
        "oi_chain": serialize_oi_chain(),
        "oi_flow_timeline": serialize_oi_flow_timeline(),
        "pre_open": serialize_pre_open(),
        "global_indices": serialize_global_indices(),
    }
