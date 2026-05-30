"""Engine-wide dataclass models — shape of state, no behavior.

Imported into GammaLeak.py via `from core.models import *` so existing
callers (`from GammaLeak import SymbolState`) keep resolving unchanged.

Only dataclasses + the `TickData` namedtuple live here. Anything with
mutable runtime intent (instances, registries, caches) belongs in core/state.py.
"""
from __future__ import annotations

from collections import deque, namedtuple
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from core.config import (
    DEQUE_MAXLEN,
    ATR_MAX_BUCKETS,
    SIGNAL_WARMING_UP,
    MACRO_SYMBOLS,
    PCR_STRIKE_STEP,
    PCR_DYNAMIC_WINDOW_OFFSETS,
)


TickData = namedtuple("TickData", ["timestamp", "ltp", "volume"])


@dataclass
class SymbolState:
    ticks: deque = field(default_factory=lambda: deque(maxlen=DEQUE_MAXLEN))
    _timestamps: np.ndarray = field(
        default_factory=lambda: np.zeros(DEQUE_MAXLEN, dtype=np.float64)
    )
    _prices: np.ndarray = field(
        default_factory=lambda: np.zeros(DEQUE_MAXLEN, dtype=np.float64)
    )
    _tick_count: int = 0

    cum_price_volume: float = 0.0
    cum_volume: int = 0
    last_vtt: int = 0
    oi: float = 0.0

    vwap: float = 0.0
    std_dev: float = 0.0
    z_score: float = 0.0
    ltp: float = 0.0
    ltp_style: str = "white"
    action_signal: str = SIGNAL_WARMING_UP
    action_style: str = "dim white"

    sig_state: int = 0
    alert_side: int = 0
    peak_z: float = 0.0
    prev_z: float = 0.0
    # MFE gating: capture entry price when ALERT fires + track running max
    # favorable excursion. CONFIRM (1→2) requires signal_mfe_points to clear
    # the per-symbol noise floor so a 2-pt liquidity sweep can't be promoted
    # to a confirmed setup.
    alert_entry_ltp: float = 0.0
    signal_mfe_points: float = 0.0

    efficiency_ratio: float = 0.0
    hurst: float = 0.5
    regime: str = "NORMAL"

    session_day: date | None = None
    session_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_finalized: bool = False
    was_outside_or: bool = False
    is_fakeout: bool = False
    last_tick_ts: float | None = None

    thesis_started_at: float | None = None
    thesis_age_secs: float = 0.0
    thesis_decay: bool = False
    regime_shift_alert: bool = False

    last_hurst_calc_ts: float | None = None
    ticks_since_hurst: int = 0

    last_vwap_touch_ts: float | None = None
    last_vwap_touch_side: int = 0
    vwap_rejection_active: bool = False

    last_signal_exit_ts: float | None = None
    vwap_slope: float = 0.0
    price_slope_5m: float = 0.0        # 5-min price slope for drift detection

    # V3.0: Gamma flush override + TPS
    gamma_flush_active: bool = False
    gamma_flush_side: int = 0
    _tps_timestamps: deque = field(default_factory=lambda: deque(maxlen=200))
    tps: float = 0.0

    # V4.0: Adaptive Regime Engine
    atr: float = 0.0                   # Current 14-period ATR on 1-min bars
    atr_session_mean: float = 0.0      # Session running mean ATR for Z-scale
    atr_ratio: float = 1.0             # atr / atr_session_mean (for threshold scaling)
    _ohlc_buckets: deque = field(default_factory=lambda: deque(maxlen=ATR_MAX_BUCKETS))
    _current_bucket_ts: float = 0.0    # Start timestamp of current 1-min bucket
    _current_bucket: dict = field(default_factory=lambda: {"o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0})
    _atr_sum: float = 0.0             # Running sum for session mean
    _atr_count: int = 0
    dynamic_regime: str = ""           # V4 regime tag: THE PIN / EXPANSION / GAMMA SQUEEZE
    implied_upper: float = 0.0         # ATM straddle-implied ceiling
    implied_lower: float = 0.0         # ATM straddle-implied floor
    straddle_premium: float = 0.0      # Combined ATM CE+PE premium
    anchor_gate: str = ""              # Cross-asset gate status
    oi_roc_ce_atm: float = 0.0        # Rolling 3-min OI RoC for ATM CE
    oi_roc_pe_atm: float = 0.0        # Rolling 3-min OI RoC for ATM PE

    # V5.1: OI Delta Flow Classification
    oi_flow_label: str = ""            # NEW LONGS / NEW SHORTS / SHORT COVER / LONG EXIT / NEUTRAL
    oi_flow_ce_pe: str = ""            # CE WRITERS IN / PE WRITERS IN / STRADDLE BUILD / UNWINDING
    # Raw deltas across ATM window (computed in classify_oi_flow). Exposed so
    # the anchored timeline chart can plot continuous Flow Velocity lines.
    oi_flow_ce_delta: int = 0
    oi_flow_pe_delta: int = 0

    # Phase 1 (institutional): per-alert cooldown + conviction + named setup
    last_alert_fire_ts: float | None = None   # when sig_state last transitioned 0→1
    last_alert_fire_side: int = 0              # side of that alert, for same-side block
    conviction_score: int = 0                  # 1–5 strength of the currently-displayed signal
    conviction_factors: str = ""               # comma-joined active factors at CONFIRM (e.g. "EXH,DIV") — logged for lift calibration
    setup_label: str = ""                      # named setup (e.g. "VWAP RECLAIM L")
    event_blackout_reason: str = ""            # non-empty if current tick is in a scheduled blackout

    # V5.2 Micro-Structural Layer (additive — does NOT feed sig_state)
    # Layer 1: 15-min rolling Micro-Z (catches flushes session Z can't see)
    micro_vwap: float = 0.0                    # 15-min rolling mean price
    micro_std_dev: float = 0.0                 # 15-min rolling σ (with ATR floor)
    micro_z_score: float = 0.0                 # (ltp - micro_vwap) / micro_std_dev
    # Layer 2: Z-velocity history + amber pre-alert
    _z_history: deque = field(default_factory=lambda: deque(maxlen=256))
    z_velocity: float = 0.0                    # dZ/dt over ZVEL_WINDOW_SECS
    amber_active: bool = False                 # early-warning tier (below sig_state)
    amber_side: int = 0                        # +1 building long, -1 building short
    amber_reason: str = ""                     # "ZVEL", "DRIVER"
    # Layer 3: Tick-arrival rate
    tick_rate_short: float = 0.0               # ticks/sec over TICKRATE_SHORT_SECS
    tick_rate_baseline: float = 0.0            # trailing 10-min baseline ticks/sec
    tick_rate_spike: bool = False              # short ≥ SPIKE_MULT × baseline

    # Order-flow (book pressure) — populated for futures only
    tbq: float = 0.0                           # total buy quantity in the book
    tsq: float = 0.0                           # total sell quantity in the book
    book_imbalance: float = 0.0                # (tbq - tsq) / (tbq + tsq); range [-1, +1]

    # Phase 1 OFI — ring buffers of the last N tbq/tsq snapshots so we can
    # compute the rolling change (ΔTBQ - ΔTSQ) as a leading-indicator on
    # passive liquidity intent. delta_ofi_smoothed and absorption_label are
    # observational fields (no engine action yet) surfaced in the card math
    # dropdown for human read. Promoted to a conviction-bumper only after
    # Phase 2 backtest validates correlation with MFE-passing CONFIRMs.
    tbq_history: deque = field(default_factory=lambda: deque(maxlen=10))
    tsq_history: deque = field(default_factory=lambda: deque(maxlen=10))
    delta_ofi_smoothed: float = 0.0            # (last_tbq-first_tbq) - (last_tsq-first_tsq)
    absorption_label: str = ""                 # BULL_ABSORB / BEAR_ABSORB / BULL_VOID / BEAR_VOID / ""

    # Aggressor-classified flow (Lee-Ready tick rule + midpoint refinement)
    cvd: int = 0                               # cumulative volume delta = Σ(buy_vol - sell_vol) since session open
    _prev_ltp_for_aggressor: float = 0.0
    _prev_vtt_for_aggressor: int = 0
    _last_aggressor: str = ""                  # last non-flat classification, used for zero-tick fallback

    # Per-minute aggressor bars
    current_minute_epoch: int = -1             # int(timestamp // 60) for the in-progress bar
    minute_buy_vol: int = 0                    # rolling: this minute's buyer-aggressor volume
    minute_sell_vol: int = 0                   # rolling: this minute's seller-aggressor volume
    last_completed_minute_buy: int = 0
    last_completed_minute_sell: int = 0
    last_completed_minute_delta: int = 0       # last bar's (buy - sell) — used for breakout confirmation
    recent_minute_deltas: deque = field(default_factory=lambda: deque(maxlen=10))

    # Divergence detection state
    session_high_tracked: float = 0.0
    session_low_tracked: float = 0.0
    cvd_at_session_high: int = 0
    cvd_at_session_low: int = 0
    # Pullback validation — a "swing high test" requires price to have pulled
    # back materially from the prior high before testing it again. Without this
    # gate, gap-day opens (where session_high_tracked is set at the open price
    # itself) produce false exhaustion fires the moment price ticks one point
    # past the open. Reset when session_high_tracked is updated; flips True
    # only after price drops 0.5 * ATR below the high.
    high_pullback_seen: bool = False
    low_pullback_seen: bool = False
    divergence_label: str = ""                 # BUYER_EXHAUSTION / SELLER_EXHAUSTION / BREAKOUT_CONFIRMED / SELL_ABSORPTION / BUY_ABSORPTION
    divergence_ts: float = 0.0                 # decay after CVD_DIVERGENCE_DECAY_SECS

    # Pre-open gap context (populated at boot / first tick of session)
    prior_close: float = 0.0                   # prior trading day's close
    gap_pct: float = 0.0                       # (today_open / prior_close - 1) * 100
    gap_bucket: str = ""                       # LARGE_GAP_UP / SMALL_GAP_UP / FLAT / SMALL_GAP_DN / LARGE_GAP_DN

    # Plain-English decision layer (display-only; does NOT gate sig_state)
    english_verdict: str = ""                  # e.g., "FADE THE BOUNCE", "STAND ASIDE"
    english_why: str = ""                      # short reason — what's driving the verdict
    english_confidence: str = ""               # LOW / MED / HIGH


@dataclass
class MacroQuote:
    value: float | None = None
    change_pct: float | None = None
    updated_at: float | None = None
    error: str | None = None


@dataclass
class MacroState:
    quotes: dict[str, MacroQuote] = field(
        default_factory=lambda: {label: MacroQuote() for label in MACRO_SYMBOLS}
    )


@dataclass
class PCRState:
    ce_oi: dict[int, float] = field(default_factory=dict)
    pe_oi: dict[int, float] = field(default_factory=dict)
    last_updated: float | None = None

    prev_ce_oi: dict[int, float] = field(default_factory=dict)
    prev_pe_oi: dict[int, float] = field(default_factory=dict)
    oi_snapshot_ts: float | None = None
    ltp_at_oi_snapshot: float = 0.0    # NIFTY LTP when OI snapshot was taken

    # V3.0: Per-strike IV and Gamma rolling history
    iv_history: dict[int, deque] = field(default_factory=dict)
    gamma_history: dict[int, deque] = field(default_factory=dict)
    tbq_by_strike: dict[int, float] = field(default_factory=dict)
    tsq_by_strike: dict[int, float] = field(default_factory=dict)

    # V4.0: Per-strike LTP + rolling OI history for Rate of Change
    ce_ltp_by_strike: dict[int, float] = field(default_factory=dict)
    pe_ltp_by_strike: dict[int, float] = field(default_factory=dict)
    oi_history_ce: dict[int, deque] = field(default_factory=dict)  # deque of (ts, oi)
    oi_history_pe: dict[int, deque] = field(default_factory=dict)  # deque of (ts, oi)

    @property
    def ce_total(self) -> float:
        return float(sum(self.ce_oi.values()))

    @property
    def pe_total(self) -> float:
        return float(sum(self.pe_oi.values()))

    @property
    def ratio(self) -> float | None:
        if self.ce_total <= 0:
            return None
        return self.pe_total / self.ce_total

    def get_dynamic_snapshot(
        self, current_nifty_ltp: float
    ) -> tuple[float | None, float, float, tuple[int, ...], int | None]:
        if current_nifty_ltp <= 0:
            return None, 0.0, 0.0, (), None

        atm_strike = int(round(current_nifty_ltp / PCR_STRIKE_STEP) * PCR_STRIKE_STEP)
        window_strikes = tuple(atm_strike + offset for offset in PCR_DYNAMIC_WINDOW_OFFSETS)
        ce_sum = float(sum(self.ce_oi.get(strike, 0.0) for strike in window_strikes))
        pe_sum = float(sum(self.pe_oi.get(strike, 0.0) for strike in window_strikes))

        if ce_sum <= 0:
            return None, ce_sum, pe_sum, window_strikes, atm_strike
        return pe_sum / ce_sum, ce_sum, pe_sum, window_strikes, atm_strike

    def get_dynamic_ratio(self, current_nifty_ltp: float) -> float | None:
        ratio, _, _, _, _ = self.get_dynamic_snapshot(current_nifty_ltp)
        return ratio


@dataclass
class DriverMetric:
    pair: tuple[str, str] = ("", "")
    corr: float = 0.0                # Pearson on 1-sec log returns, zero lag
    lead_lag_secs: int = 0           # + => first leads, - => second leads
    lead_corr: float = 0.0           # correlation at best lag
    drag: str = ""                   # "", "DRAG", "BOOST", "DIVERGE"
    drag_detail: str = ""
    n_points: int = 0
    stale: bool = True


@dataclass
class IndexDriverState:
    metrics: list[DriverMetric] = field(default_factory=list)
    last_refresh_ts: float = 0.0
    # V5.2 Layer 4: cross-asset acceleration amber (HDFCBANK/RELIANCE → NIFTY)
    nifty_driver_amber: bool = False
    nifty_driver_amber_side: int = 0           # +1/-1
    nifty_driver_amber_source: str = ""        # display name of the component that fired
    nifty_driver_amber_velocity: float = 0.0   # dZ/dt of the source at fire time


@dataclass
class OIWall:
    strike: int = 0
    oi: float = 0.0
    dist_pct: float = 0.0              # signed % distance from spot


@dataclass
class OILevels:
    symbol: str = ""                   # "NIFTY"
    spot: float = 0.0
    expiry: str = ""
    max_pain: int = 0
    max_pain_dist_pct: float = 0.0     # signed % from spot
    ce_walls: list[OIWall] = field(default_factory=list)
    pe_walls: list[OIWall] = field(default_factory=list)
    n_strikes: int = 0
    stale: bool = True
    stale_reason: str = ""


@dataclass
class OILevelsState:
    levels: dict[str, OILevels] = field(default_factory=dict)
    last_refresh_ts: float = 0.0
