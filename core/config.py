"""Engine-wide configuration constants — single source of truth.

Imported into GammaLeak.py via `from core.config import *`, which keeps
every existing `from GammaLeak import HURST_THRESHOLD` style call working
without modification. Runtime-mutable state (INSTRUMENT_KEYS, DISPLAY_NAMES,
SYMBOL_PROFILES, etc.) intentionally lives elsewhere — anything that changes
after boot is not config, it's state.
"""
from __future__ import annotations

import json
from datetime import date, timedelta, timezone
from pathlib import Path


# Runtime-learned gate, emitted by calibrate.py as a data artifact (not a
# generated .py). Path is resolved relative to this file so it's CWD-independent.
_LEARNED_GATE_PATH = Path(__file__).resolve().parent.parent / "data" / "learned_gate.json"


def _load_learned_gate() -> dict:
    """Read data/learned_gate.json (calibrate.py output).

    Returns {} when the file is absent or unreadable so the hand fallbacks
    stand — same graceful-degrade the old `from core.calibration import ...`
    had, now over a durable data file instead of a generated module.
    """
    try:
        with _LEARNED_GATE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --------------------------- TIME / SESSION ---------------------------

IST = timezone(timedelta(hours=5, minutes=30))
WARMUP_HOUR, WARMUP_MINUTE = 9, 20

SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE = 9, 15
OPENING_RANGE_END_HOUR, OPENING_RANGE_END_MINUTE = 9, 30
OPENING_DISCOVERY_END_HOUR, OPENING_DISCOVERY_END_MINUTE = 9, 30
EUROPE_WATCH_HOUR, EUROPE_WATCH_MINUTE = 12, 30
THESIS_DECAY_SECS = 45 * 60


# --------------------------- CLOSING AUCTION SESSION (CAS) ---------------------------
#
# SEBI circular 2026-01-16, live 2026-08-03 on NSE + BSE: F&O-eligible stocks
# stop continuous trading (CTS) at 15:15 and close through a call auction,
# index and stock F&O trade on to 15:40, and expiring index derivatives settle
# on the CAS-derived index close. Verified on 42/42 sessions 2026-08-03..10-01
# (research/study_cas_closing_auction.py): NIFTY / BANKNIFTY / SENSEX hold a
# flat 15:15 value until the official close prints in the 15:28/15:29 bar,
# and the feed keeps delivering that frozen value at heartbeat cadence —
# which collapses rolling σ and drives |Z| to the cap within ~12 minutes.
# SEBI's 2026-09-12 consultation may move these windows again (CAS 15:31-15:40
# with F&O to 15:45, or CAS 15:15-15:25 with F&O to 15:30), so every boundary
# lives here: a timetable change is a config edit, not a code hunt.
CAS_EFFECTIVE_FROM = date(2026, 8, 3)
PRE_CAS_CLOSE = (15, 30)            # pre-CAS: cash, index and F&O all closed at 15:30
CAS_REF_WINDOW_START = (15, 0)      # auction reference price = VWAP of 15:00-15:15 trades
CAS_CTS_END = (15, 15)              # F&O-stock CTS ends; spot indices stop computing
CAS_ORDER_ENTRY_START = (15, 20)    # 15:15-15:20 transition: no new orders
CAS_LIMIT_ONLY_START = (15, 25)     # market orders locked from here
CAS_RANDOM_CLOSE_START = (15, 28)   # order entry ends at a random time in 15:28-15:30
CAS_MATCHING_START = (15, 30)
CAS_MATCHING_END = (15, 35)         # closing prices confirmed by here
FNO_CLOSE = (15, 40)                # index/stock F&O close (pre-CAS: 15:30)

# Display-name classes for session-end logic (display names, not instrument
# keys, so the engine and calibrate.py share one rule). Spot indices are
# computed from constituent trades, so they freeze with CTS; every cash
# equity this engine tracks is an F&O stock (CAS Category I). Index futures
# trade to FNO_CLOSE. Anything unlisted (CRUDEOIL on MCX, VIX) keeps its own
# session and is never cut here.
INDEX_SPOT_DISPLAYS = frozenset({"NIFTY", "BANKNIFTY", "SENSEX"})
CAS_EQUITY_DISPLAYS = frozenset({"RELIANCE", "HDFCBANK", "SBIN", "ICICIBANK"})
INDEX_FUT_DISPLAYS = frozenset({"NIFTY_FUT", "BN_FUT", "SENSEX_FUT"})


def cas_active(day: date) -> bool:
    return day >= CAS_EFFECTIVE_FROM


def continuous_session_end(display: str, day: date) -> tuple[int, int] | None:
    """(hour, minute) at which `display` stops printing continuous-trading
    prices on `day`, or None for instruments this rule doesn't govern.

    Past this point a spot index is a frozen or auction-computed value and an
    F&O stock is an auction or post-close print — neither may drive σ/Z or
    grade an outcome. Pre-CAS days keep the old 15:30 close for every class,
    which also stops post-close heartbeat ticks from inflating Z (2026-07-05:
    |Z| hit the cap within 15 min of the close on a flat NIFTY).
    """
    if display in INDEX_SPOT_DISPLAYS or display in CAS_EQUITY_DISPLAYS:
        return CAS_CTS_END if cas_active(day) else PRE_CAS_CLOSE
    if display in INDEX_FUT_DISPLAYS:
        return FNO_CLOSE if cas_active(day) else PRE_CAS_CLOSE
    return None


def cas_phase(now_ist) -> str:
    """Closing-auction sub-phase for a tz-aware IST datetime; '' outside
    15:00-15:40 or before CAS went live."""
    if not cas_active(now_ist.date()):
        return ""
    cur = (now_ist.hour, now_ist.minute)
    if cur < CAS_REF_WINDOW_START or cur >= FNO_CLOSE:
        return ""
    if cur < CAS_CTS_END:
        return "REFERENCE"          # the auction's reference VWAP is forming
    if cur < CAS_ORDER_ENTRY_START:
        return "TRANSITION"
    if cur < CAS_LIMIT_ONLY_START:
        return "ORDER_ENTRY"
    if cur < CAS_RANDOM_CLOSE_START:
        return "LIMIT_ONLY"
    if cur < CAS_MATCHING_START:
        return "RANDOM_CLOSE"
    if cur < CAS_MATCHING_END:
        return "MATCHING"
    return "POST_CAS_FNO"           # F&O still trading, settlement already known


# --------------------------- NETWORK / FEED ---------------------------

WS_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
WS_TICK_TIMEOUT_SECS = 30  # If no WS message during market hours for this long, force reconnect.
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/{key}/{interval}/{to_date}/{from_date}"
UPSTOX_INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
LOG_DIR = Path("logs")

# Futures listed on more than one venue: symbol -> (venue preference order,
# monthly contracts only). Without this get_active_expiry_key takes the
# nearest expiry anywhere, which resolved CRUDEOIL to NSE_COM's thin twin
# (MCX names its contract "CRUDE OIL", which the exact name filter used to
# drop). Venue rank wins over expiry: a liquid preferred-venue contract beats
# an earlier-dated one elsewhere.
FUT_VENUE_PREFERENCE: dict[str, tuple[tuple[str, ...], bool]] = {
    "CRUDEOIL": (("MCX_FO", "NSE_COM"), False),
}


# --------------------------- ROLLING WINDOWS / UI CADENCE ---------------------------

DEQUE_MAXLEN = 16384  # Covers ~60m of live ticks at the expected desk rate.
ROLLING_WINDOW_SECS = 60  # Core short-term window for intraday SD/Z updates.
REFRESH_PER_SECOND = 4
TOP_N_ROWS = 10
FOCUS_TOP_N = 3


# --------------------------- LOGGING ---------------------------

REQUIRED_LOG_COLUMNS = (
    "timestamp",
    "symbol",
    "ltp",
    "vwap",
    "std_dev",
    "z_score",
    "signal",
)
LOG_COLUMNS_REQUIRED = REQUIRED_LOG_COLUMNS
LOG_COLUMNS = REQUIRED_LOG_COLUMNS + (
    "er", "hurst", "regime", "volume", "oi",
    "book_imb", "gap_pct", "gap_bucket", "verdict",
    "cvd", "min_buy", "min_sell", "divergence",
    # B2 (2026-07): exchange last-trade time (epoch ms) + last trade qty —
    # appended LAST so positional consumers (calibrate's ltp=row[2]) survive.
    # Boot-time schema rotation archives pre-bump files automatically.
    "ltt", "ltq",
)
EVENT_LOG_COLUMNS = (
    "timestamp", "timestamp_ist", "symbol", "event_type", "side",
    "z_score", "ltp", "regime", "setup_label", "conviction",
    # Phase 2: comma-joined active conviction factors (e.g. "EXH,DIV") at the
    # CONFIRM. Lets calibrate.py measure each factor's MFE lift and re-weight.
    "conv_factors",
    # Shadow conditioning features (observational — nothing gates on these yet;
    # calibrate.py measures their conditional hit rates nightly):
    #   tod_phase — session phase at the event (OPEN/MORNING/LUNCH/EUROPE/CLOSE)
    #   toxicity  — trailing |ΔCVD|/ΔVolume flow-toxicity ratio ("" when the
    #               symbol has no volume feed and no futures mirror)
    #   gex       — NIFTY net dealer gamma over the subscribed ATM window at
    #               event time (SPX sign convention, see orderflow/gex.py;
    #               "" until the option chain has delivered live greeks)
    "tod_phase", "toxicity", "gex",
)
# Per-strike dealer-gamma snapshot written every GEX_SNAPSHOT_SECS to
# logs/YYYY-MM-DD_gex.csv. Long format (one row per strike per snapshot) so
# the sign model can be re-derived offline without re-collecting anything.
GEX_LOG_COLUMNS = (
    "timestamp", "timestamp_ist", "spot", "strike",
    "gamma", "ce_oi", "pe_oi", "ce_delta", "pe_delta", "net_gex_1pct",
    # Per-strike premium (last option LTP each side; "" if not yet traded).
    # ΔOI alone cannot attribute flow — every contract has a buyer AND a
    # writer. ΔOI × Δpremium disambiguates: OI↑+prem↓ = writers in,
    # OI↑+prem↑ = buyers in, OI↓+prem↑ = writer buy-back (squeeze fuel),
    # OI↓+prem↓ = long unwind. Offline classifier consumes these snapshots;
    # readers must be column-count-aware (rows before 2026-07-06 are 10-col).
    "ce_ltp", "pe_ltp",
)
# Per-sample snapshot of the OI Flow Anchored Velocity Chart's underlying
# state. Persisting these lets us retroactively verify "did spot bounce off
# the PE wall?" or "did a PE-delta spike lead a price reversal?" — the chart
# itself is in-memory only, so without this file the answer is unknowable.
# Same 5s cadence as the in-memory ring buffer.
OI_STATE_LOG_COLUMNS = (
    "timestamp", "timestamp_ist", "spot", "fut",
    "top_ce_strike", "top_pe_strike", "max_pain",
    "ce_delta", "pe_delta", "oi_flow_label", "oi_flow_ce_pe",
)
LOG_BATCH_SIZE = 5  # Flush every 5 ticks (~100-500ms) to prevent data loss
LOG_FLUSH_INTERVAL_SECS = 1.0

REVIEW_DEFAULT_WINDOW_MINUTES = 15
MACRO_POLL_INTERVAL_SECS = 60
REPLAY_ROLLING_WINDOW_SECS = 3600  # 60m window for 1-min candle data
REPLAY_TICK_DELAY = 0.01


# --------------------------- HURST ---------------------------

HURST_LIVE_RECALC_SECS = 5.0
HURST_REPLAY_RECALC_TICKS = 10
HURST_LIVE_WINDOW_SECS = 3600  # 60m structural window for live Hurst estimation
HURST_THRESHOLD = 0.55         # H above this → persistent/trending
REGIME_SHIFT_HURST_THRESHOLD = 0.60


# --------------------------- MACRO BIAS ---------------------------

# PCR is the only term since USDINR was removed (2026-10-01). Its feed was
# dead in every logged session, so the label history is unchanged: any
# non-zero PCR direction already cleared the ±0.25 thresholds on its own.
MACRO_BIAS_WEIGHTS = {
    "PCR": 1.0,
}
MACRO_BIAS_FLAT_CHANGE_PCT = 0.05
PCR_BULLISH_THRESHOLD = 1.10
PCR_BEARISH_THRESHOLD = 0.90
MACRO_BIAS_BULLISH_THRESHOLD = 0.25
MACRO_BIAS_BEARISH_THRESHOLD = -0.25


# --------------------------- SIGNAL ENGINE (Confirmation Hook State Machine) ---------------------------

SIGNAL_ALERT_Z = 3.0           # |Z| threshold to enter Alert state
SIGNAL_CONFIRM_Z = 2.5         # |Z| must cross back inside this to confirm
SIGNAL_EXIT_Z = 1.0            # |Z| below this exits Execution state
SIGNAL_EXHAUSTION_PEAK = 4.0   # |peak_z| above this → exhaustion-grade signal


# --------------------------- KAUFMAN EFFICIENCY RATIO ---------------------------

ER_LOOKBACK_SECS = 4200        # 70-minute lookback (~14 x 5m bars) to suppress chop false positives
ER_TREND_THRESHOLD = 0.6       # ER above this → trending, suppress mean-reversion


# --------------------------- INDIA VIX ---------------------------

VIX_INSTRUMENT_KEY = "NSE_INDEX|India VIX"
# BSE SENSEX spot — stable key, present in Upstox master under name="SENSEX".
# SENSEX_FUT is resolved dynamically (BSE_FO|SENSEX<YY><MMM>FUT). The matching
# logic in get_active_expiry_key relies on the `name` column to disambiguate
# from SENSEX50 (lot 75) contracts which share the trading-symbol prefix.
SENSEX_INDEX_KEY = "BSE_INDEX|SENSEX"
VIX_HIGH_THRESHOLD = 18.0       # Above this: widen Z-thresholds (fear regime)
VIX_LOW_THRESHOLD = 12.0        # Below this: tighten Z-thresholds (complacency)
VIX_HIGH_SCALE = 1.30           # Multiply Z-thresholds by 1.3x in fear
VIX_LOW_SCALE = 0.80            # Shrink Z-thresholds by 0.8x in complacency
VIX_CRUSH_THRESHOLD = -8.0      # VIX drop > 8% intraday = vol crush
VIX_SPIKE_THRESHOLD = 15.0      # VIX rise > 15% intraday = risk-off event


# --------------------------- PHASE 1: MATH FOUNDATION ---------------------------

SD_FLOOR_ATR_FRACTION = 0.001
SD_FLOOR_ATR_MULTIPLIER = 0.50
SD_FLOOR_LONGWIN_SECS = 300
SD_MIN_ABSOLUTE = 0.01
Z_SCORE_CAP = 10.0


# --------------------------- PHASE 2: SIGNAL LOGIC ---------------------------

THESIS_HARD_KILL_SECS = 20 * 60
THESIS_WARN_SECS = 10 * 60
THESIS_URGENT_SECS = 15 * 60


# --------------------------- PHASE 3: BEHAVIORAL UPGRADES ---------------------------

VWAP_TOUCH_Z_THRESHOLD = 0.5
VWAP_REJECTION_COOLDOWN_SECS = 300
SIGNAL_COOLDOWN_SECS = 180
SAME_SIDE_ALERT_COOLDOWN_SECS = 180


# --------------------------- INDEX DRIVER PANEL ---------------------------

INDEX_DRIVER_LOOKBACK_SECS = 300
INDEX_DRIVER_LAG_MAX_SECS = 30
INDEX_DRIVER_LAG_STEP_SECS = 5
INDEX_DRIVER_REFRESH_SECS = 15
INDEX_DRIVER_MIN_POINTS = 60
INDEX_DRIVER_PAIRS: list[tuple[str, str]] = [
    ("NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"),
    ("NSE_INDEX|Nifty 50", "NSE_EQ|RELIANCE"),
    ("NSE_INDEX|Nifty Bank", "NSE_EQ|HDFCBANK"),
    # SENSEX correlates tightly with NIFTY (overlapping constituents). Use it as
    # a confluence/cross-check on direction — divergence between the two is rare
    # and meaningful when it shows up.
    ("NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX"),
]


# --------------------------- OI LEVELS PANEL ---------------------------

OI_LEVELS_REFRESH_SECS = 15
OI_LEVELS_BAND_PCT = 0.03
OI_LEVELS_WALLS_COUNT = 3
OI_LEVELS_MIN_STRIKES = 5
OI_LEVELS_STALE_SECS = 120


# --------------------------- V5.2 MICRO-STRUCTURAL LAYER ---------------------------

# Layer 1: Rolling 15-min Micro-Z
MICRO_Z_WINDOW_SECS = 900
MICRO_Z_MIN_POINTS = 30
MICRO_Z_SD_ATR_FRAC = 0.15

# Layer 2: Z-velocity pre-alert (amber)
ZVEL_WINDOW_SECS = 30
ZVEL_AMBER_THRESHOLD = 0.05
ZVEL_MIN_Z = 0.7
ZVEL_MAX_Z = 2.0

# Layer 3: Tick-arrival-rate spike
TICKRATE_SHORT_SECS = 10
TICKRATE_BASELINE_SECS = 600
TICKRATE_SPIKE_MULT = 2.0
TICKRATE_MIN_BASELINE_HZ = 0.5

# Layer 4: Driver acceleration amber
DRIVER_ACCEL_THRESHOLD = 0.06
DRIVER_ACCEL_NIFTY_MAX_Z = 1.5
DRIVER_ACCEL_SOURCES = ("NSE_EQ|HDFCBANK", "NSE_EQ|RELIANCE")


# --------------------------- EVENT CALENDAR / BLACKOUTS ---------------------------

EVENT_BLACKOUT_PRE_MINS = 15
EVENT_BLACKOUT_POST_MINS = 15

SCHEDULED_EVENTS: list[tuple[str, str]] = [
    ("2026-04-17T10:00:00+05:30", "RBI Monetary Policy"),
    ("2026-04-17T18:00:00+05:30", "US CPI Release"),
    ("2026-04-22T19:30:00+05:30", "FOMC Minutes"),
    ("2026-04-23T14:00:00+05:30", "OPEC+ Meeting"),
]
EVENT_FILE_PATH = "data/events.txt"
# FALLBACK ONLY — the engine keys expiry logic to the RESOLVED NIFTY expiry
# date from the instrument master (analytics.vol_surface.get_expiry_date).
# This weekday constant is used only when no expiry has been resolved yet
# (preflight / mock mode). Verified against the cached master 2026-07-03:
# every NIFTY option expiry is a TUESDAY (Mon=0 → 1). The old value 3
# (Thursday) predated the Sept-2025 exchange expiry reshuffle and mislabeled
# every EXPIRY PIN in logs through 2026-06 (all fired on Thursdays).
EXPIRY_BLACKOUT_WEEKDAY = 1          # Tuesday (Mon=0) — fallback only
EXPIRY_BLACKOUT_START = (15, 0)
# Through the F&O close: under CAS expiring contracts trade until 15:40, with
# settlement already fixed by the auction from ~15:30. Pre-CAS nothing traded
# after 15:30, so the longer window changes nothing for older sessions.
EXPIRY_BLACKOUT_END = FNO_CLOSE


# --------------------------- NAMED SETUP LABELS ---------------------------

SETUP_VWAP_RECLAIM_L = "VWAP RECLAIM L"
SETUP_VWAP_RECLAIM_S = "VWAP RECLAIM S"
SETUP_ORB_BREAK_L = "ORB BREAK L"
SETUP_ORB_BREAK_S = "ORB BREAK S"
SETUP_EXHAUSTION_REV_L = "EXHAUSTION REV L"
SETUP_EXHAUSTION_REV_S = "EXHAUSTION REV S"
SETUP_FADE_HIGH_L = "FADE HIGH L"
SETUP_FADE_HIGH_S = "FADE HIGH S"
SETUP_FADE_LOW_L = "FADE LOW L"
SETUP_FADE_LOW_S = "FADE LOW S"
SETUP_EXPIRY_PIN = "EXPIRY PIN"


# --------------------------- V3.0: GAMMA FLUSH DETECTION ---------------------------

GAMMA_FLUSH_IV_SPIKE_PCT = 0.15       # IV must spike >15% above 3min mean
GAMMA_FLUSH_GAMMA_EXPAND_PCT = 0.20   # Gamma must expand >20% above 3min mean
GAMMA_FLUSH_SELL_DOMINANCE = 0.65     # TSQ / (TBQ+TSQ) > this
GAMMA_FLUSH_WINDOW_SECS = 180         # 3-minute rolling window
GAMMA_FLUSH_HISTORY_MAXLEN = 360      # ~3min at 2 ticks/sec


# --------------------------- PHASE 5: VOL SURFACE ---------------------------

VOL_SAMPLE_INTERVAL_SECS: float = 5.0   # how often to recompute ATM IV + skew
VOL_IV_HISTORY_MAXLEN: int = 360        # deque depth per strike (~3min at 2 ticks/sec, matches gamma flush)


# --------------------------- V4.0: ADAPTIVE REGIME ENGINE ---------------------------

ATR_PERIOD = 14
ATR_BUCKET_SECS = 60
ATR_MAX_BUCKETS = 120

OI_ROC_WINDOW_SECS = 180
OI_ROC_CAPITULATION_PCT = -8.0
OI_ROC_PIN_RANGE = (-2.0, 2.0)
OI_ROC_HISTORY_MAXLEN = 720

STRADDLE_BOX_ENABLED = True
CROSS_DIVERGENCE_Z_THRESHOLD = -0.5

REGIME_PIN = "THE PIN"
REGIME_EXPANSION = "EXPANSION"
REGIME_GAMMA_SQUEEZE = "GAMMA SQUEEZE"
REGIME_ANCHOR_DIVERGE = "ANCHOR DIVERGENCE"

ATR_Z_SCALE_ENABLED = True
ATR_Z_SCALE_FLOOR = 0.6
ATR_Z_SCALE_CAP = 2.0
TPS_WINDOW_SECS = 1.0
TPS_HURST_SUSPEND_THRESHOLD = 50


# --------------------------- V5.0: FII/DII + SONAR NEWS ---------------------------

FII_BOOT_ENABLED = True
SONAR_ENABLED = True
SONAR_COOLDOWN_SECS = 300
SONAR_SIGNAL_TRIGGER_Z = 3.0
SONAR_QUERY_INSTRUMENTS = {"NIFTY", "BANKNIFTY", "CRUDEOIL", "RELIANCE", "HDFCBANK"}


# --------------------------- PCR BOOTSTRAP ---------------------------
#
# NOTE: PCR_EXPIRY_CODE deliberately stays in GammaLeak.py because the
# bootloader REBINDS it at runtime (`PCR_EXPIRY_CODE = nifty_expiry`). A rebind
# in Desktop's namespace wouldn't propagate back to core.config, so callers
# importing it from here would see the stale "26MAY" default forever.

PCR_BASE_STRIKE = 22500
PCR_STRIKE_STEP = 50
PCR_WING_COUNT = 10
PCR_DYNAMIC_WINDOW_OFFSETS = (-100, -50, 0, 50, 100)


# --------------------------- SIGNAL NAME STRINGS ---------------------------

SIGNAL_WARMING_UP = "WARMING UP"
SIGNAL_NO_EDGE = "NO EDGE"
SIGNAL_STRETCH = "STRETCH"
SIGNAL_FADE_SCALP_LONG = "FADE → SCALP LONG"
SIGNAL_FADE_SCALP_SHORT = "FADE → SCALP SHORT"
SIGNAL_EXHAUSTION_SCALP_LONG = "EXHAUSTION → SCALP LONG"
SIGNAL_EXHAUSTION_SCALP_SHORT = "EXHAUSTION → SCALP SHORT"
SIGNAL_CONFIRMED_FADE = "CONFIRMED FADE"
SIGNAL_CONFIRMED_EXHAUSTION = "CONFIRMED FADE - EXHAUSTION"
SIGNAL_TREND_STAND_DOWN = "TREND REGIME - STAND DOWN"
SIGNAL_REGIME_SHIFT = "REGIME SHIFT - STAND DOWN"
SIGNAL_BREAKOUT_ATTEMPT = "BREAKOUT ATTEMPT"
SIGNAL_FAKEOUT_PULLBACK = "FAKEOUT PULLBACK"
SIGNAL_MOMENTUM_LONG = "MOMENTUM → LONG"
SIGNAL_MOMENTUM_SHORT = "MOMENTUM → SHORT"
SIGNAL_GAMMA_FLUSH_LONG = "GAMMA FLUSH → LONG"
SIGNAL_GAMMA_FLUSH_SHORT = "GAMMA FLUSH → SHORT"
SIGNAL_MACRO_ALIGNED = "MACRO ALIGNED"
SIGNAL_DRIFT_STAND_DOWN = "DRIFT — STAND DOWN"
SIGNAL_DRIFT_ALIGNED = "DRIFT ALIGNED"

SIGNAL_CONFIRMED_SET = {
    SIGNAL_FADE_SCALP_LONG, SIGNAL_FADE_SCALP_SHORT,
    SIGNAL_EXHAUSTION_SCALP_LONG, SIGNAL_EXHAUSTION_SCALP_SHORT,
}

SIGNAL_ABBREVIATIONS = {
    SIGNAL_FADE_SCALP_LONG: "SCALP L",
    SIGNAL_FADE_SCALP_SHORT: "SCALP S",
    SIGNAL_EXHAUSTION_SCALP_LONG: "EX L",
    SIGNAL_EXHAUSTION_SCALP_SHORT: "EX S",
    SIGNAL_MOMENTUM_LONG: "MOM L",
    SIGNAL_MOMENTUM_SHORT: "MOM S",
    SIGNAL_CONFIRMED_FADE: "FADE",
    SIGNAL_CONFIRMED_EXHAUSTION: "FADE X",
}


# --------------------------- REVIEW / CURRENCY ---------------------------

REVIEW_ENTRY_TOUCH = "touch"
REVIEW_ENTRY_CONFIRM = "confirm"
REVIEW_ENTRY_MODES = (REVIEW_ENTRY_TOUCH, REVIEW_ENTRY_CONFIRM)

# Minimum favorable excursion (in instrument points) acting as the FLOOR for
# the MFE threshold. The runtime threshold is max(K × ATR, floor), so on a
# volatile day the bar scales up but never drops below this number — protects
# against rewarding micro-grabs even when the engine briefly sees a quiet
# regime. Tuned from 6 sessions (May 12–19 2026) of historical per-symbol
# avg-MFE distributions:
#   CRUDE 30 → 15 (avg MFE was 21.5, 30 was unreachable in normal regimes)
#   RELIANCE 5 → 3, HDFCBANK 8 → 3 (those equities don't move enough; tighter
#     floor surfaces what little real edge exists)
# VIX stays at 999 (display-only by design).
MIN_FAVORABLE_POINTS_PER_SYMBOL: dict[str, float] = {
    "NIFTY":      15.0,
    "NIFTY_FUT":  15.0,
    "BANKNIFTY":  40.0,
    "BN_FUT":     40.0,
    "SENSEX":     50.0,   # SENSEX ~3x NIFTY in absolute pts; lot 20 → 50pt floor
    "SENSEX_FUT": 50.0,   # mirror of spot floor; tighten after a few sessions if data warrants
    "RELIANCE":    3.0,
    "HDFCBANK":    3.0,
    "SBIN":        3.0,
    "ICICIBANK":   5.0,
    "CRUDEOIL":   15.0,
    "VIX":       999.0,
}
# Fallback for any symbol not in the table above (~0.05% of entry price).
REVIEW_DEFAULT_MIN_FAVORABLE_PCT = 0.05

# ATR-scaled MFE threshold: runtime_bar = max(MFE_ATR_K × atr, floor)
# K=0.5 means a "meaningful move" must clear half the recent 5-min range. On
# a calm day (low ATR) the floor dominates; on a volatile day the bar scales
# up so a 2-pt favorable tick in a 30-pt range still reads as noise — but
# the bar doesn't demand a full-range reversion (which empirically rejected
# too many real signals on index futures).
MFE_ATR_K = 0.5
# Lookback window for the ATR proxy in the review path (the per-tick CSV
# doesn't carry an atr column, so we compute max-min range over the prior
# N seconds of price ticks at signal time).
MFE_ATR_PROXY_WINDOW_SECS = 300


# --------------------------- REGIME GATE ---------------------------
# Block or elevate the bar for (setup, regime) pairs that empirically
# underperform on MFE-graded outcomes. Rules are checked at the 1→2
# sig_state promotion (and at the MFE-retry promotion). A blocked
# promotion becomes an ABORT event with the gate reason captured in
# the setup_label slot of the events.csv row.
#
# Source: 4-day rolling MFE-grade table (2026-05-19..22).
#   ORB BREAK L | NORMAL       = 4/17  = 24%  -> BLOCK
#   EXHAUSTION REV S | NORMAL  = 5/19  = 26%  -> require conviction ≥ 4
# Whitelist (no rule needed, free pass): any *DIVERGE regime (4/5 = 80%).
#
# Tuple format: (setup_label_exact, regime_substring, action).
# action ∈ {"BLOCK", "REQUIRE_CONV_<n>"}
#
# HAND_REGIME_RULES is the fallback used when data/learned_gate.json is absent or a
# (setup, regime) pair is too thin for the nightly grader to have jurisdiction.
# The effective SETUP_REGIME_RULES is the merge (see _merge_regime_rules).
# Source of the hand rules: 4-day window 2026-05-19..22 — kept only as a floor;
# `python calibrate.py` re-derives the live table from all sessions' outcomes.
HAND_REGIME_RULES: list[tuple[str, str, str]] = [
    ("ORB BREAK L",      "NORMAL", "BLOCK"),
    ("EXHAUSTION REV S", "NORMAL", "REQUIRE_CONV_4"),
]


def _merge_regime_rules() -> list[tuple[str, str, str]]:
    """Effective gate = hand rules, with calibrate.py taking over any pair it has
    jurisdiction over (>= MIN_SAMPLE_GATE outcomes).

    The merge is asymmetric — tighten aggressively, relax conservatively:
      - CALIBRATED_REGIME_RULES (BLOCK/REQUIRE) replace/add, when the grader has
        a clear underperformer with enough sample.
      - CALIBRATED_RELAX drops a hand rule ONLY for a setup that cleanly recovered
        (>=40% on >= MIN_SAMPLE_GATE outcomes) — e.g. EXHAUSTION REV S climbing
        from 26% to 54%. A merely mediocre setup never auto-undoes a human block.
      - Pairs the grader can't speak to (thin / WATCH band) keep the hand rule.
    """
    effective: dict[tuple[str, str], str] = {
        (s, r): a for s, r, a in HAND_REGIME_RULES
    }
    learned = _load_learned_gate()
    for pair in learned.get("relax", []):            # recovered setup — un-gate it
        if len(pair) == 2:
            effective.pop((pair[0], pair[1]), None)
    for rule in learned.get("regime_rules", []):
        s, r, a = rule.get("setup"), rule.get("regime"), rule.get("action")
        if s and r and a:
            effective[(s, r)] = a
    return [(s, r, a) for (s, r), a in effective.items()]


SETUP_REGIME_RULES: list[tuple[str, str, str]] = _merge_regime_rules()


def evaluate_regime_gate(setup_label: str, regime: str, conviction: int) -> tuple[bool, str]:
    """Check (setup, regime) against the rule table.

    Returns (allowed, reason). reason is "" when allowed.
    Reason format on block: "<setup>|<regime>" so it lands in the ABORT
    event row cleanly and is searchable in events.csv.
    """
    if not setup_label or not regime:
        return True, ""
    for rule_setup, rule_regime, action in SETUP_REGIME_RULES:
        if setup_label == rule_setup and rule_regime in regime:
            if action == "BLOCK":
                return False, f"{rule_setup}|{rule_regime}"
            if action.startswith("REQUIRE_CONV_"):
                required = int(action.rsplit("_", 1)[-1])
                if conviction < required:
                    return False, f"{rule_setup}|{rule_regime}|CONV<{required}"
            break
    return True, ""


# --------------------------- CONVICTION FACTOR WEIGHTS ---------------------------
# The 1–5 conviction score is base 1 + the summed weight of each active factor,
# capped to [1, 5] (see compute_conviction_score). Flat +1-per-factor was found
# non-predictive (conv-4 hit 42% vs conv-3's 50% over 8 sessions) — the score
# only means something if the weights track each factor's realised MFE lift.
#
# HAND defaults carry the 2026-05-31 first-pass lift read; calibrate.py overrides
# any factor it has enough sample to measure (>= MIN_FACTOR_SAMPLE present AND
# absent). Factor keys match the tags compute_conviction_score emits into
# events.csv's conv_factors column.
HAND_CONVICTION_WEIGHTS: dict[str, int] = {
    "EXH":   2,   # |peak_z| >= exhaustion_peak — measured +21 lift (n=99), dominant
    "DIV":   1,   # CVD divergence aligns with the fade — measured +12 lift (n=46)
    "CHOP":  1,   # ER & Hurst both non-trending (fade-friendly) — weak +3, kept
    "OIF":   1,   # OI flow aligns with fade side — not yet instrumented, kept neutral
    "DRIFT": 0,   # measured -37pp lift (7% hit present vs 44% absent, n=14/122,
                  # 16-session grade 2026-07-03) — anti-predictive; zeroed until a
                  # future calibration run with n>=15 present says otherwise
    "OFI":   0,   # Phase-1 OFI absorption aligns — instrumented, off until validated
    "VOL":   0,   # Phase-5 low-IV + non-FEAR skew — instrumented, off until calibrated
}


def _merge_conviction_weights() -> dict[str, int]:
    """HAND weights, with calibrate.py overriding any factor it has measured.

    Pure lift-derived (hit-with-factor minus hit-without, graded against the
    fixed MFE floor) so — unlike the floors — this is non-circular and safe to
    auto-apply. Factors the grader hasn't instrumented enough keep their hand
    weight. Regenerate with: python calibrate.py
    """
    weights = dict(HAND_CONVICTION_WEIGHTS)
    for factor, w in _load_learned_gate().get("conviction_weights", {}).items():
        try:
            weights[factor] = int(w)
        except (TypeError, ValueError):
            pass
    return weights


CONVICTION_FACTOR_WEIGHTS: dict[str, int] = _merge_conviction_weights()


# --------------------------- SHADOW CONDITIONING (P1 toxicity / P3 time-of-day) ---------------------------
#
# Observational features logged on every sig_state event so the nightly grader
# can measure conditional hit rates BEFORE anything gates on them (shadow →
# paper → live promotion path). Definitions must stay in lockstep with the
# retroactive study scripts so live and research measurements are comparable.

# Flow toxicity: TOX(t) = |CVD(t) − CVD(t−W)| / (Vol(t) − Vol(t−W)).
# A cheap VPIN-style proxy on the existing aggressor stream — high values mean
# one-sided (informed / parent-order) flow, the tape where fades die.
TOX_WINDOW_SECS = 1200        # 20-min trailing window
TOX_SNAPSHOT_SECS = 30        # (ts, cvd, cum_volume) snapshot cadence

# Net dealer gamma exposure (P2 research track). Snapshot cadence + staleness
# bound for a strike's last-seen gamma. Window-local by construction: only the
# subscribed ATM±window strikes contribute (wings excluded — noted limitation).
GEX_SNAPSHOT_SECS = 60.0      # one per-strike snapshot row set per minute
GEX_STALE_SECS = 120.0        # ignore a strike's gamma older than this
GEX_MIN_STRIKES = 3           # need at least this many live strikes to publish

# Expiry settlement-anchor tracker (P4 research track, shadow). On the
# resolved NIFTY expiry day, from EXPIRY_TRACK_START the tracker samples the
# gamma-mass pin strike; from EXPIRY_SETTLE_START it also maintains a running
# estimate of the exchange settlement price and samples faster. Pre-CAS the
# settlement was the last-half-hour average of the index (a running mean
# locks it in progressively — the original inefficiency). Since 2026-08-03 it
# is the single CAS-derived close, so the estimate becomes the auction's
# implied close (orderflow/cas.py) and then the printed close itself; the
# averaging mechanism the P4 hypothesis relied on no longer exists, so pre-
# and post-CAS expiries must never be pooled. Rows: logs/YYYY-MM-DD_expiry.csv.
EXPIRY_TRACK_START = (13, 0)          # pin observation begins (IST)
EXPIRY_SETTLE_START = (15, 0)         # settlement window opens
EXPIRY_SETTLE_END = (15, 30)          # pre-CAS close; CAS days track to FNO_CLOSE
EXPIRY_SAMPLE_SECS_PRE = 60.0         # snapshot cadence 13:00–15:00
EXPIRY_SAMPLE_SECS_SETTLE = 15.0      # snapshot cadence inside the window
EXPIRY_LOG_COLUMNS = (
    "timestamp", "timestamp_ist", "spot",
    "settle_est",       # settlement estimate ("" before the window; see settle_src)
    "settle_n",         # ticks in the running mean (pre-CAS MEAN source only)
    "pin_strike",       # argmax gamma-mass strike (see pin_src)
    "pin_src",          # GEX (gamma x OI) | OI (OI-only fallback, no live greeks)
    "pin_mass",         # gamma-mass (or OI) at the pin strike
    "pin_dist",         # reference - pin_strike (points); reference = spot,
                        # or settle_est inside a CAS day's window (2026-10+)
    "mins_to_close",    # minutes until the settlement price is fixed
    # Appended 2026-10 (CAS): MEAN (pre-CAS running average) | SPOT (CAS day,
    # before the freeze) | SYNTH / FUT (auction implied close) | FROZEN | CLOSE
    # (official CAS close printed). Older files lack the column.
    "settle_src",
)

# CAS analytics log (orderflow/cas.py, shadow): logs/YYYY-MM-DD_cas.csv. SNAP
# rows every CAS_SAMPLE_SECS from CAS_LOG_START to FNO_CLOSE per index, plus
# one SUMMARY row per index once the close prints (or at the F&O close).
CAS_LOG_START = (15, 10)
CAS_SAMPLE_SECS = 15.0
CAS_SYNTH_STRIKES = 3                 # nearest strikes in the put-call-parity forward
CAS_SYNTH_MAX_AGE_SECS = 60.0         # option LTPs older than this are ignored
CAS_LOG_COLUMNS = (
    "timestamp", "timestamp_ist", "row_type", "index", "phase",
    "spot", "last_cts", "frozen", "official_close", "print_ist",
    "fut", "fut_cts", "fut_implied_close",
    "synth", "synth_cts", "synth_implied_close",
    "implied_gap", "implied_src", "realized_gap", "realized_gap_bps",
    "expiry", "nse_eq_status", "nse_fo_status",
    # NIFTY rows only (the subscribed chain): the level the chain is read
    # against (orderflow.cas.derivatives_spot — SPOT | FUT | FROZEN), its ATM
    # strike, and the feed's call / put IV there. Same-strike call and put IV
    # should agree; if Upstox prices greeks off the frozen index they split
    # as the futures move through the auction — this pair is the check.
    "chain_spot", "chain_src", "atm_strike", "iv_ce_atm", "iv_pe_atm",
)
# Segment status transitions from the feed's market_info frames (PRE_OPEN_*,
# NORMAL_OPEN/CLOSE, CLOSING_START/END) — the exchange's own phase clock,
# logged so the CAS timetable above can be verified against it.
MARKET_STATUS_LOG_COLUMNS = ("timestamp", "timestamp_ist", "segment", "status")
# Display context for the dashboard's CAS panel only (gates nothing): the
# realised close-vs-15:15 gap distribution from
# research/study_cas_closing_auction.py, 42 sessions 2026-08-03..10-01.
# Refresh by re-running that study.
CAS_GAP_REFERENCE: dict[str, dict[str, float]] = {
    "NIFTY":     {"mean_abs_pts": 38.0, "mean_abs_bps": 15.8, "p90_abs_bps": 30.7, "n": 42},
    "BANKNIFTY": {"mean_abs_pts": 105.0, "mean_abs_bps": 18.4, "p90_abs_bps": 36.0, "n": 42},
    "SENSEX":    {"mean_abs_pts": 102.4, "mean_abs_bps": 13.4, "p90_abs_bps": 28.5, "n": 42},
}

# B3: L5 depth logging (shadow, research-only). The "full" feed already
# delivers 5 levels per side; the engine keeps only L1 in memory. Every
# marketFF tick for a core (symbol_states) instrument appends one row to
# logs/YYYY-MM-DD/<SYM>.depth.csv — dot in the stem so the tick-writer schema
# sentinel and the integrity census both ignore it. Buffered writes; a crash
# loses at most the last DEPTH_FLUSH_SECS of depth (never tick data).
# Purpose: real queue-imbalance / sweep detection to replace the failed
# L1-only toxicity proxy, and spoof/absorption research.
DEPTH_LOG_ENABLED = True
DEPTH_FLUSH_ROWS = 100        # per-symbol buffer flush threshold
DEPTH_FLUSH_SECS = 10.0       # ... or at least this often (global check)
DEPTH_LOG_COLUMNS = (
    "timestamp",
    "bp1", "bq1", "ap1", "aq1",
    "bp2", "bq2", "ap2", "aq2",
    "bp3", "bq3", "ap3", "aq3",
    "bp4", "bq4", "ap4", "aq4",
    "bp5", "bq5", "ap5", "aq5",
)

# Session phases for time-of-day conditioning (IST, half-open [start, end)).
SESSION_PHASES: tuple[tuple[tuple[int, int], tuple[int, int], str], ...] = (
    ((9, 15), (10, 0), "OPEN"),      # retail-heavy price discovery
    ((10, 0), (11, 30), "MORNING"),  # institutional main session
    ((11, 30), (13, 0), "LUNCH"),    # volume trough, algo-dominated
    ((13, 0), (14, 0), "EUROPE"),    # European open overlap
    ((14, 0), (15, 30), "CLOSE"),    # MTM / positioning flows
)
# From CAS_EFFECTIVE_FROM: CLOSE ends at CTS end and the auction window is
# its own phase (spot frozen, F&O still live). Pre-CAS sessions keep the old
# table so their tod_phase labels stay comparable with the retroactive
# studies — but a CLOSE read must never pool the two eras.
SESSION_PHASES_CAS: tuple[tuple[tuple[int, int], tuple[int, int], str], ...] = (
    ((9, 15), (10, 0), "OPEN"),
    ((10, 0), (11, 30), "MORNING"),
    ((11, 30), (13, 0), "LUNCH"),
    ((13, 0), (14, 0), "EUROPE"),
    ((14, 0), CAS_CTS_END, "CLOSE"),
    (CAS_CTS_END, FNO_CLOSE, "CAS"),
)


def session_phase(now_ist) -> str:
    """Phase label for a tz-aware IST datetime; '' outside session hours."""
    cur = now_ist.hour * 60 + now_ist.minute
    phases = SESSION_PHASES_CAS if cas_active(now_ist.date()) else SESSION_PHASES
    for (sh, sm), (eh, em), label in phases:
        if sh * 60 + sm <= cur < eh * 60 + em:
            return label
    return ""


# --------------------------- OI FLOW TIMELINE ---------------------------

OI_FLOW_MIN_DELTA = 500                  # Minimum net OI change to classify (avoid noise)
OI_FLOW_TIMELINE_WINDOW_SECS = 30 * 60   # 30 min visible on the anchored velocity chart
OI_FLOW_TIMELINE_SAMPLE_SECS = 5         # downsample to one row / 5s


# --------------------------- MACRO LABEL REGISTRY ---------------------------
#
# Empty by default (GIFT NIFTY removed — pre-open check is manual). Lives here
# rather than in state because nothing ever REASSIGNS or mutates it; if a future
# macro source is added, it'd be a code change here, not a runtime mutation.
# MacroState's default_factory closes over this name, so it must be importable
# from core/ to avoid circular imports between core/models and GammaLeak.
MACRO_SYMBOLS: dict[str, str] = {}
