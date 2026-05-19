"""Engine-wide configuration constants — single source of truth.

Imported into GammaLeak.py via `from core.config import *`, which keeps
every existing `from GammaLeak import HURST_THRESHOLD` style call working
without modification. Runtime-mutable state (INSTRUMENT_KEYS, DISPLAY_NAMES,
SYMBOL_PROFILES, etc.) intentionally lives elsewhere — anything that changes
after boot is not config, it's state.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path


# --------------------------- TIME / SESSION ---------------------------

IST = timezone(timedelta(hours=5, minutes=30))
WARMUP_HOUR, WARMUP_MINUTE = 9, 20

SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE = 9, 15
OPENING_RANGE_END_HOUR, OPENING_RANGE_END_MINUTE = 9, 30
OPENING_DISCOVERY_END_HOUR, OPENING_DISCOVERY_END_MINUTE = 9, 30
EUROPE_WATCH_HOUR, EUROPE_WATCH_MINUTE = 12, 30
THESIS_DECAY_SECS = 45 * 60


# --------------------------- NETWORK / FEED ---------------------------

WS_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
WS_TICK_TIMEOUT_SECS = 30  # If no WS message during market hours for this long, force reconnect.
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle/{key}/{interval}/{to_date}/{from_date}"
UPSTOX_INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
LOG_DIR = Path("logs")


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
)
EVENT_LOG_COLUMNS = (
    "timestamp", "timestamp_ist", "symbol", "event_type", "side",
    "z_score", "ltp", "regime", "setup_label", "conviction",
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

USDINR_FLAT_MOVE = 0.08
MACRO_BIAS_WEIGHTS = {
    "USDINR": 0.35,
    "PCR": 0.65,
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


# --------------------------- USDINR / RBI INTERVENTION ---------------------------

RBI_INTERVENTION_LEVELS = (92.50, 93.50)   # suspected ceiling/floor
RBI_PROXIMITY = 0.05                        # 5 paise
USDINR_Z_BOOST = 0.80                       # multiply Z thresholds near RBI levels


# --------------------------- INDIA VIX ---------------------------

VIX_INSTRUMENT_KEY = "NSE_INDEX|India VIX"
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
EXPIRY_BLACKOUT_WEEKDAY = 3          # Thursday (Mon=0)
EXPIRY_BLACKOUT_START = (15, 0)
EXPIRY_BLACKOUT_END = (15, 30)


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
SONAR_QUERY_INSTRUMENTS = {"NIFTY", "BANKNIFTY", "CRUDEOIL", "USDINR", "RELIANCE", "HDFCBANK"}


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
    "RELIANCE":    3.0,
    "HDFCBANK":    3.0,
    "SBIN":        3.0,
    "ICICIBANK":   5.0,
    "USDINR":      0.05,
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
UPSTOX_CURRENCY_UNDERLYINGS = ("USDINR", "EURINR", "GBPINR", "JPYINR")


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
