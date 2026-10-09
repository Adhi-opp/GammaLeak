"""Closing Auction Session (CAS) analytics — shadow: display + logging only.

Since 2026-08-03 NSE/BSE close F&O-eligible stocks through a call auction
(timetable in core/config.py). Spot indices stop computing at CTS end and
hold that value until the CAS-derived close prints (15:28-15:30; verified
flat on 42/42 sessions), while index futures and options keep trading to the
F&O close. Expiring index contracts settle on that auction close.

Per index (NIFTY / BANKNIFTY / SENSEX) this module tracks:
  last_cts, frozen     last continuous print, and the 15:15 value the index
                       holds through the auction
  official_close       the auction close: the first post-freeze print that
                       differs from `frozen` (or `frozen` itself if nothing
                       has changed by CAS_MATCHING_END)
  fut_implied_close    futures now minus the basis fixed at the freeze — the
                       F&O market's running estimate of the auction close
  synth_implied_close  NIFTY only: put-call-parity forward (median of
                       K + C − P over the nearest strikes) minus its carry at
                       the freeze — the same estimate read off the option book
  implied_gap          implied close − frozen (SYNTH preferred, else FUT)

derivatives_spot() is the level index F&O trades against: the live spot, or
inside the CAS window the last continuous spot plus the futures' move since
the freeze. orderflow/chain.py reads the NIFTY option chain against it, and
NIFTY rows here log it with the feed's ATM call/put IV (frozen-greeks check).

Nothing here gates a signal; the _cas.csv log exists so hypotheses about the
auction can be tested on forward data.

Hooks: on_spot_tick / on_fut_tick from record_tick (every tick, frozen or
not) and on_market_info from the feed receiver. Writes are wrapped — a disk
error never reaches the tick path.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime

from core.config import (
    CAS_CTS_END,
    CAS_GAP_REFERENCE,
    CAS_LOG_START,
    CAS_MATCHING_END,
    CAS_ORDER_ENTRY_START,
    CAS_RANDOM_CLOSE_START,
    CAS_REF_WINDOW_START,
    CAS_SAMPLE_SECS,
    CAS_SYNTH_MAX_AGE_SECS,
    CAS_SYNTH_STRIKES,
    FNO_CLOSE,
    cas_active,
    cas_phase,
)
from analytics.vol_surface import _latest_iv
from core.models import CASIndexState
from core.state import cas_state, market_status, pcr_state
from gammaleak_runtime.io_logs import append_cas_row, append_market_status_row


INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
_FUT_TO_INDEX = {"NIFTY_FUT": "NIFTY", "BN_FUT": "BANKNIFTY", "SENSEX_FUT": "SENSEX"}
_PRINT_EPS = 0.005            # index values print to 2 dp
_SYNTH_REFRESH_SECS = 5.0

_expiry_dates: dict[str, date] = {}
_last_sample_ts: float = 0.0
_last_synth_ts: float = 0.0


def _hm(t: datetime) -> tuple[int, int]:
    return (t.hour, t.minute)


def set_expiry_dates(dates: dict[str, date | None]) -> None:
    """Index display name -> nearest option expiry, set by the bootloader.
    Today's flag is applied when the day's tracker is created."""
    for index, d in dates.items():
        if d is not None:
            _expiry_dates[index] = d
    today_trackers = [(i, s) for i, s in cas_state.items() if s.day is not None]
    for index, s in today_trackers:
        s.expiry = _expiry_dates.get(index) == s.day


def _state_for(index: str, day: date) -> CASIndexState:
    s = cas_state.get(index)
    if s is None or s.day != day:
        s = CASIndexState(day=day, expiry=(_expiry_dates.get(index) == day))
        cas_state[index] = s
    return s


# ----------------------------------------------------------------- estimators

def parity_forward(now: float, spot: float) -> float | None:
    """Median of K + C − P over the CAS_SYNTH_STRIKES strikes nearest `spot`
    whose call AND put both quoted within CAS_SYNTH_MAX_AGE_SECS.

    Front-expiry NIFTY options only (the subscribed window). Carry between
    the forward and spot is removed by the caller, so the absolute level
    needn't be discounted.
    """
    if spot <= 0:
        return None
    ce, pe = pcr_state.ce_ltp_by_strike, pcr_state.pe_ltp_by_strike
    vals: list[float] = []
    for k in sorted(set(ce) & set(pe), key=lambda k: abs(k - spot)):
        c, p = ce.get(k, 0.0), pe.get(k, 0.0)
        if c <= 0.0 or p <= 0.0:
            continue
        if (now - pcr_state.ce_ltp_ts_by_strike.get(k, 0.0) > CAS_SYNTH_MAX_AGE_SECS
                or now - pcr_state.pe_ltp_ts_by_strike.get(k, 0.0) > CAS_SYNTH_MAX_AGE_SECS):
            continue
        vals.append(k + c - p)
        if len(vals) >= CAS_SYNTH_STRIKES:
            break
    return statistics.median(vals) if vals else None


def _refresh_synth(s: CASIndexState, ts: float, cur: tuple[int, int]) -> None:
    global _last_synth_ts
    if ts - _last_synth_ts < _SYNTH_REFRESH_SECS:
        return
    _last_synth_ts = ts
    # Strikes nearest the futures-implied level once the cash index freezes.
    near = s.fut_now - s.basis_cts if (s.basis_cts is not None and s.fut_now) else s.spot
    f = parity_forward(ts, near)
    if f is None:
        return
    if cur < CAS_CTS_END:
        s.synth_cts = f
    s.synth_now = f


def _update_implied(s: CASIndexState, cur: tuple[int, int]) -> None:
    """Carry-adjusted implied closes, valid from the freeze onward. Basis and
    carry are fixed once (at the first post-freeze update) against the last
    continuous print, so a moving futures/forward reads as auction drift."""
    if cur < CAS_CTS_END or not s.frozen:
        return
    anchor = s.last_cts or s.frozen
    if s.basis_cts is None and s.fut_cts:
        s.basis_cts = s.fut_cts - anchor
    if s.carry_cts is None and s.synth_cts:
        s.carry_cts = s.synth_cts - anchor
    if s.basis_cts is not None and s.fut_now:
        s.fut_implied_close = s.fut_now - s.basis_cts
    if s.carry_cts is not None and s.synth_now:
        s.synth_implied_close = s.synth_now - s.carry_cts
    if s.synth_implied_close:
        best, src = s.synth_implied_close, "SYNTH"
    elif s.fut_implied_close:
        best, src = s.fut_implied_close, "FUT"
    else:
        return
    s.implied_gap = best - s.frozen
    s.implied_src = src
    # The forecast that gets scored: last estimate before the earliest
    # possible close print, untouched by the print itself.
    if cur < CAS_RANDOM_CLOSE_START:
        s.implied_close_at_lock = best


def derivatives_spot(index: str, spot_ltp: float, now_ist: datetime) -> tuple[float, str]:
    """The index level the derivatives complex is trading against: (value, src).

    SPOT    continuous trading (or not a CAS day, or after the F&O close) —
            the live index print
    FUT     CAS window, CTS end -> F&O close: last continuous spot plus the
            futures' move since the freeze (fut_now - basis fixed at 15:15).
            The cash index is frozen and then prints the auction close, but
            index futures and options trade on to 15:40, and the options
            price off the futures: on 12 sessions (2026-09-15..10-01) the
            option-implied forward moved with the futures in 5/5 minutes
            where the futures moved 10+ pts against a frozen spot.
    FROZEN  CAS window but no basis yet (no futures tick on both sides of
            the freeze) — falls back to the cash print
    """
    if not cas_active(now_ist.date()):
        return spot_ltp, "SPOT"
    cur = _hm(now_ist)
    if cur < CAS_CTS_END or cur >= FNO_CLOSE:
        return spot_ltp, "SPOT"
    s = cas_state.get(index)
    if s is not None and s.day == now_ist.date() and s.basis_cts is not None and s.fut_now:
        return s.fut_now - s.basis_cts, "FUT"
    return spot_ltp, "FROZEN"


def settlement_estimate(index: str, now_ist: datetime) -> tuple[float, str] | None:
    """Best current estimate of today's CAS close: (value, source) where
    source is CLOSE | SYNTH | FUT | FROZEN; None before the freeze."""
    s = cas_state.get(index)
    if s is None or s.day != now_ist.date():
        return None
    if s.official_close:
        return s.official_close, "CLOSE"
    if not s.frozen:
        return None
    if s.implied_src == "SYNTH" and s.synth_implied_close:
        return s.synth_implied_close, "SYNTH"
    if s.fut_implied_close:
        return s.fut_implied_close, "FUT"
    return s.frozen, "FROZEN"


# ----------------------------------------------------------------- tick hooks

def on_spot_tick(index: str, ltp: float, ts: float, now_ist: datetime) -> None:
    """Every spot tick for a CAS index (frozen or not)."""
    if ltp <= 0 or index not in INDICES or not cas_active(now_ist.date()):
        return
    cur = _hm(now_ist)
    if cur < CAS_REF_WINDOW_START or cur >= FNO_CLOSE:
        return              # the day's auction record stays as it was at the F&O close
    s = _state_for(index, now_ist.date())
    s.spot = ltp
    if cur < CAS_CTS_END:
        s.last_cts = ltp
    else:
        # Capture the freeze value only before the earliest possible print —
        # a restart after the print must leave it unknown, not mislabel the
        # close as the freeze.
        if not s.frozen and cur < CAS_RANDOM_CLOSE_START:
            s.frozen = ltp
        if s.frozen and not s.official_close:
            if cur >= CAS_ORDER_ENTRY_START and abs(ltp - s.frozen) > _PRINT_EPS:
                s.official_close = ltp
                s.print_ts = ts
            elif cur >= CAS_MATCHING_END:
                s.official_close = s.frozen      # auction closed exactly at the freeze
                s.print_ts = ts
    if index == "NIFTY":
        _refresh_synth(s, ts, cur)
    _update_implied(s, cur)
    _maybe_log(ts, now_ist)


def on_fut_tick(fut_display: str, ltp: float, ts: float, now_ist: datetime) -> None:
    """Every index-futures tick; NIFTY_FUT / BN_FUT / SENSEX_FUT map to their index."""
    index = _FUT_TO_INDEX.get(fut_display)
    if index is None or ltp <= 0 or not cas_active(now_ist.date()):
        return
    cur = _hm(now_ist)
    if cur < CAS_REF_WINDOW_START or cur >= FNO_CLOSE:
        return
    s = _state_for(index, now_ist.date())
    if cur < CAS_CTS_END:
        s.fut_cts = ltp
    s.fut_now = ltp
    _update_implied(s, cur)


def on_market_info(segment_status: dict[str, str], ts: float) -> None:
    """Feed market_info frame (segment -> status name). Logs transitions only;
    the first frame after a (re)connect logs every segment's current state."""
    for seg, status in segment_status.items():
        if market_status.get(seg) == status:
            continue
        market_status[seg] = status
        try:
            append_market_status_row(ts, seg, status)
        except Exception:
            pass


# ----------------------------------------------------------------- logging

def _chain_fields(index: str, s: CASIndexState, ts: float, now_ist: datetime) -> dict:
    """NIFTY only (the subscribed chain): the level the chain is read against
    and the feed's ATM call / put IV there — the frozen-index greeks check
    (see CAS_LOG_COLUMNS). First live read: 2026-10-05."""
    if index != "NIFTY" or not s.spot:
        return {}
    level, src = derivatives_spot(index, s.spot, now_ist)
    atm = int(round(level / 50.0) * 50)
    return {
        "chain_spot": level, "chain_src": src, "atm_strike": atm,
        "iv_ce_atm": _latest_iv(pcr_state.iv_history_ce.get(atm), ts),
        "iv_pe_atm": _latest_iv(pcr_state.iv_history_pe.get(atm), ts),
    }


def _append(ts: float, row_type: str, index: str, phase: str, s: CASIndexState,
            now_ist: datetime) -> None:
    try:
        append_cas_row(ts, row_type, index, phase, s, market_status,
                       _chain_fields(index, s, ts, now_ist))
    except Exception:
        pass


def _maybe_log(ts: float, now_ist: datetime) -> None:
    global _last_sample_ts
    cur = _hm(now_ist)
    if cur < CAS_LOG_START or cur >= FNO_CLOSE:
        return
    day = now_ist.date()
    phase = cas_phase(now_ist)
    if ts - _last_sample_ts >= CAS_SAMPLE_SECS:
        _last_sample_ts = ts
        for index in INDICES:
            s = cas_state.get(index)
            if s is not None and s.day == day and s.spot:
                _append(ts, "SNAP", index, phase, s, now_ist)
    for index in INDICES:
        s = cas_state.get(index)
        if s is not None and s.day == day and s.official_close and not s.summary_logged:
            s.summary_logged = True
            _append(ts, "SUMMARY", index, phase, s, now_ist)


# ----------------------------------------------------------------- dashboard

def serialize(now_ist: datetime) -> dict:
    """Dashboard payload. `rows` is empty outside the day's auction window
    (before 15:00) — the panel renders the timetable and waits."""
    day = now_ist.date()
    rows = []
    for index in INDICES:
        s = cas_state.get(index)
        if s is None or s.day != day:
            continue
        ref = CAS_GAP_REFERENCE.get(index, {})
        realized = (s.official_close - s.frozen) if (s.official_close and s.frozen) else None
        forecast_err = (s.official_close - s.implied_close_at_lock
                        if (s.official_close and s.implied_close_at_lock) else None)
        rows.append({
            "index": index,
            "expiry": s.expiry,
            "spot": round(s.spot, 2) if s.spot else None,
            "last_cts": round(s.last_cts, 2) if s.last_cts else None,
            "frozen": round(s.frozen, 2) if s.frozen else None,
            "official_close": round(s.official_close, 2) if s.official_close else None,
            "fut_implied_close": round(s.fut_implied_close, 2) if s.fut_implied_close else None,
            "synth_implied_close": round(s.synth_implied_close, 2) if s.synth_implied_close else None,
            "implied_gap": round(s.implied_gap, 2) if s.implied_gap is not None else None,
            "implied_gap_bps": (round(1e4 * s.implied_gap / s.frozen, 1)
                                if (s.implied_gap is not None and s.frozen) else None),
            "implied_src": s.implied_src,
            "realized_gap": round(realized, 2) if realized is not None else None,
            "realized_gap_bps": round(1e4 * realized / s.frozen, 1) if realized is not None else None,
            "forecast_err": round(forecast_err, 2) if forecast_err is not None else None,
            "ref_mean_abs_pts": ref.get("mean_abs_pts"),
            "ref_p90_abs_bps": ref.get("p90_abs_bps"),
            "ref_n": ref.get("n"),
        })
    return {
        "active": cas_active(day),
        "phase": cas_phase(now_ist),
        "rows": rows,
        "market_status": dict(market_status),
    }
