"""Live implied volatility surface metrics from the Upstox option chain feed.

Computes three metrics on a sample timer (VOL_SAMPLE_INTERVAL_SECS):
  - ATM IV   : mean of CE and PE implied vol at the ATM strike
  - 25d skew : put_IV(ATM - SKEW_OFFSET) − call_IV(ATM + SKEW_OFFSET)
               positive value = put premium = fear/defensive buying pressure
  - IV pct   : where ATM IV sits in the rolling session-open history (0–100)

IV values arrive directly from the Upstox protobuf feed (MarketFF.iv field) so
no Black-Scholes inversion is required. Values are in decimal form (0.14 = 14%).

Session-open ATM IV (sampled at 09:20 IST) is appended to
logs/iv_session_history.csv so the percentile improves each day without
requiring any external data.
"""
from __future__ import annotations

import csv
from collections import deque
from datetime import datetime
from pathlib import Path

from core.config import IST, LOG_DIR

VOL_SAMPLE_INTERVAL_SECS: float = 5.0
VOL_MAX_IV_AGE_SECS: float = 30.0      # stale threshold for a single IV sample
VOL_SKEW_OFFSET: int = 100             # pts OTM for the skew proxy (2 strikes on NIFTY 50-pt grid)
VOL_IV_HISTORY_FILE: str = "iv_session_history.csv"
VOL_IV_HISTORY_MAX_SESSIONS: int = 30  # rolling window for percentile

_IV_SAMPLE_HOUR = 9
_IV_SAMPLE_MINUTE = 20                 # sample session-open ATM IV at 09:20 IST

_iv_history_cache: list[float] = []
_history_loaded: bool = False
_session_iv_date: str = ""             # date of the last appended session-open entry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest_iv(history: deque | None, now: float) -> float | None:
    """Return the most-recent IV from a (ts, iv) deque if within max age."""
    if not history:
        return None
    ts, iv = history[-1]
    if (now - ts) > VOL_MAX_IV_AGE_SECS or iv <= 0.0:
        return None
    return float(iv)


def _load_history() -> list[float]:
    global _iv_history_cache, _history_loaded
    if _history_loaded:
        return _iv_history_cache
    path = LOG_DIR / VOL_IV_HISTORY_FILE
    rows: list[float] = []
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if len(row) >= 2:
                        try:
                            rows.append(float(row[1]))
                        except ValueError:
                            pass
        except OSError:
            pass
    _iv_history_cache = rows[-VOL_IV_HISTORY_MAX_SESSIONS:]
    _history_loaded = True
    return _iv_history_cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_atm_iv(nifty_ltp: float, now: float, iv_history_ce: dict, iv_history_pe: dict) -> float | None:
    """Mean of CE and PE implied vol at the ATM strike. Returns None if both absent."""
    atm = int(round(nifty_ltp / 50.0) * 50)
    ce_iv = _latest_iv(iv_history_ce.get(atm), now)
    pe_iv = _latest_iv(iv_history_pe.get(atm), now)
    if ce_iv is not None and pe_iv is not None:
        return (ce_iv + pe_iv) / 2.0
    return ce_iv if ce_iv is not None else pe_iv


def get_skew(
    nifty_ltp: float, now: float,
    iv_history_ce: dict, iv_history_pe: dict,
    offset: int = VOL_SKEW_OFFSET,
) -> float | None:
    """OTM put IV (ATM - offset) minus OTM call IV (ATM + offset).
    Positive = put premium = fear. None if either side is stale/absent."""
    atm = int(round(nifty_ltp / 50.0) * 50)
    put_iv = _latest_iv(iv_history_pe.get(atm - offset), now)
    call_iv = _latest_iv(iv_history_ce.get(atm + offset), now)
    if put_iv is None or call_iv is None:
        return None
    return put_iv - call_iv


def get_iv_percentile(current_iv: float, history: list[float]) -> float:
    """0–100 percentile of current_iv within the session-open history."""
    if len(history) < 3:
        return 50.0
    below = sum(1 for v in history if v <= current_iv)
    return (below / len(history)) * 100.0


def maybe_record_session_iv(atm_iv: float, session_day_str: str, now: float) -> None:
    """Append session-open ATM IV to the history file once per session at 09:20 IST."""
    global _iv_history_cache, _session_iv_date
    if session_day_str == _session_iv_date:
        return
    ts_ist = datetime.fromtimestamp(now, IST)
    if ts_ist.hour != _IV_SAMPLE_HOUR or ts_ist.minute < _IV_SAMPLE_MINUTE:
        return
    path = LOG_DIR / VOL_IV_HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        csv.writer(fh).writerow([session_day_str, f"{atm_iv:.6f}"])
    _iv_history_cache.append(atm_iv)
    if len(_iv_history_cache) > VOL_IV_HISTORY_MAX_SESSIONS:
        _iv_history_cache = _iv_history_cache[-VOL_IV_HISTORY_MAX_SESSIONS:]
    _session_iv_date = session_day_str


def load_history() -> list[float]:
    """Public accessor — returns the (possibly cached) session-open IV history."""
    return _load_history()
