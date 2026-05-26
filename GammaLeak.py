"""
GammaLeak Engine V2.0 
Single-file intraday VWAP z-score monitor with Async Math Pipeline and Dynamic ATM Radar.
"""

import os
import sys

# When this file is run as `python GammaLeak.py`, Python registers it
# under the module name `__main__` — NOT `GammaLeak`. Sub-modules in
# ui/, orderflow/, signals/ etc. that back-import `GammaLeak` would
# otherwise trigger a fresh load (and a circular import while top-level code
# is still running). Aliasing here makes those back-imports resolve to the
# already-running __main__ module.
if __name__ == "__main__" and "GammaLeak" not in sys.modules:
    sys.modules["GammaLeak"] = sys.modules["__main__"]

os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

import argparse
import asyncio
import csv
import gzip
import io
import json
import random
import re
import ssl
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
import numpy as np
import pandas as pd
import websockets
from dotenv import load_dotenv
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as pb


load_dotenv()

# --- V5.0: FII/DII + Sonar News Integration ---
try:
    from fii_dii_scraper import fetch_latest_fii_data, FIISnapshot
    _FII_AVAILABLE = True
except ImportError:
    _FII_AVAILABLE = False

try:
    from sonar_news import SonarNewsEngine, NewsContext
    _SONAR_AVAILABLE = True
except ImportError:
    _SONAR_AVAILABLE = False


# --------------------------- CONFIGURATION ---------------------------

ACCESS_TOKEN: str = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()

# All engine-wide config constants now live in core/config.py. This single
# `import *` reproduces every previous module-scope name so existing callers
# (`from GammaLeak import HURST_THRESHOLD`, `import GammaLeak as X`)
# continue to resolve unchanged.
from core.config import *  # noqa: F401, F403
# Dataclasses (SymbolState, PCRState, etc.) and shared state singletons
# (symbol_states, pcr_state, oi_levels_state, oi_flow_timeline, DISPLAY_NAMES, ...)
# pulled in early so subsequent module-level initialization (SYMBOL_PROFILES at
# line below, etc.) can reference them without ordering surprises.
from core.models import *  # noqa: F401, F403
from core.state import *  # noqa: F401, F403

# --- Runtime state that lived inside the old constants block (kept here) ---

# Shutdown hardening: Track if graceful shutdown is in progress
_GRACEFUL_SHUTDOWN_IN_PROGRESS = False

# USDINR active-month key — bootloader mutates this in place via .clear()/.add()
# once the live expiry is resolved; the seed value is intentionally stale.
USDINR_KEYS: set[str] = {"NSE_FO|USDINR26MAYFUT"}

# These will be dynamically overwritten by the Bootloader on startup
INSTRUMENT_KEYS: list[str] = [
    "NSE_INDEX|Nifty 50",
    "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|India VIX",
    "NSE_FO|USDINR26MAYFUT",
    "MCX_FO|CRUDEOIL26MAYFUT",
    "NSE_EQ|RELIANCE",
    "NSE_EQ|HDFCBANK",
]

# DISPLAY_NAMES moved to core/state.py (bootloader-mutated initial-state dict —
# same pattern as symbol_states / pcr_state). Re-exported into this namespace
# by `from core.state import *` above.

DEFAULT_SYMBOL_PROFILE = {
    "alert_z": SIGNAL_ALERT_Z,
    "confirm_z": SIGNAL_CONFIRM_Z,
    "exit_z": SIGNAL_EXIT_Z,
    "exhaustion_peak": SIGNAL_EXHAUSTION_PEAK,
    "er_trend_threshold": ER_TREND_THRESHOLD,
    "hurst_trend_threshold": HURST_THRESHOLD,
}
SYMBOL_PROFILES: dict[str, dict[str, float]] = {
    key: dict(DEFAULT_SYMBOL_PROFILE) for key in DISPLAY_NAMES
}
FOCUS_PRIORITY: dict[str, int] = {
    "NSE_INDEX|Nifty Bank": 0,
    "NSE_INDEX|Nifty 50": 1,
    "BSE_INDEX|SENSEX": 1,  # tied with NIFTY — they correlate tightly
    "NSE_FO|USDINR26MAYFUT": 2,
    "MCX_FO|CRUDEOIL26MAYFUT": 3,
    "NSE_EQ|HDFCBANK": 4,
    "NSE_EQ|RELIANCE": 5,
    "NSE_INDEX|India VIX": 99,  # VIX is context, not a signal target
}

# Symbols subscribed and ingested (macro bias / RBI / catalyst still depend on them)
# but NOT rendered as instrument cards on the main grid. Use the Macro header instead.
HIDDEN_FROM_CARDS: set[str] = set()

PCR_EXPIRY_CODE = "26MAY"  # Will be resolved dynamically in bootloader (rebound — must stay local)
# PCR_BASE_STRIKE / PCR_STRIKE_STEP / PCR_WING_COUNT / PCR_DYNAMIC_WINDOW_OFFSETS
# now live in core/config.py and arrive via `from core.config import *`.

_instrument_master_by_symbol: dict[str, list[dict[str, str]]] | None = None
_historical_key_cache: dict[str, str] = {}


# --------------------------- CORE RESOLUTION ---------------------------

async def get_active_expiry_key(symbol: str, instrument_type: str = "FUT"):
    """Resolves current active contract using the LOCAL Instrument Master.
    Zero-latency, no REST API call — works offline and during off-market hours.
    Returns tuple: (instrument_key, expiry_code) or (None, None) if not found.
    """
    global _instrument_master_by_symbol

    if _instrument_master_by_symbol is None:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await load_upstox_instrument_master(session)
        except Exception:
            return (None, None)

    if _instrument_master_by_symbol is None:
        return (None, None)

    today = str(date.today())
    valid_contracts = []

    symbol = symbol.upper()

    # Filter Master.  Use a real derivative-symbol match instead of a loose
    # startswith check: NIFTY must not accidentally match NIFTYNXT50, etc.
    for tradingsymbol, instruments in _instrument_master_by_symbol.items():
        if not derivative_tradingsymbol_matches(symbol, tradingsymbol, instrument_type):
            continue

        for m in instruments:
            key = m.get("instrument_key", "")
            # Exact underlying disambiguation. The tradingsymbol regex catches
            # most lookalikes (NIFTYNXT50 vs NIFTY) but SENSEX vs SENSEX50 share
            # a prefix and BOTH start with a digit after the underlying, so the
            # regex alone can't separate them. The `name` column does.
            if m.get("name", "") and m["name"] != symbol:
                continue
            # Filter Options vs Futures
            if instrument_type == "OPT" and ("CE" not in tradingsymbol and "PE" not in tradingsymbol):
                continue
            if instrument_type == "FUT" and "FUT" not in tradingsymbol:
                continue

            expiry = m.get("expiry", "")
            # FUT: skip expiry-day contracts (liquidity has rolled to next month;
            # MCX especially blocks/thins the expiring contract intraday).
            # OPT: keep expiry-day inclusive — weekly/monthly expiry options are
            # heavily traded on their expiry day (gamma pin, PCR, max-pain stay valid).
            if expiry:
                alive = (expiry > today) if instrument_type == "FUT" else (expiry >= today)
                if alive:
                    valid_contracts.append({
                        'trading_symbol': tradingsymbol,
                        'exchange': m.get('exchange', ''),
                        'expiry': expiry,
                        'instrument_key': key
                    })

    if not valid_contracts:
        return (None, None)

    # Pick the front-month (nearest expiry)
    valid_contracts.sort(key=lambda x: x["expiry"])
    best_match = valid_contracts[0]
    trading_sym = best_match['trading_symbol']
    expiry_code = None

    # CRITICAL FIX: Extract exact internal expiry string (e.g. 26APR or 26409)
    if instrument_type == "OPT":
        # Match anything between SYMBOL and STRIKE+CE/PE
        pattern = rf"^{symbol}(.*?)(\d{{4,5}})(?:CE|PE)$"
        match = re.match(pattern, trading_sym)
        if match:
            expiry_code = match.group(1)
    else:
        # Match anything between SYMBOL and FUT
        pattern = rf"^{symbol}(.*?)FUT$"
        match = re.match(pattern, trading_sym)
        if match:
            expiry_code = match.group(1)

    # Fallback: parse from expiry date field
    if not expiry_code and best_match.get("expiry"):
        try:
            expiry_dt = datetime.strptime(best_match["expiry"], "%Y-%m-%d")
            expiry_code = expiry_dt.strftime("%d%b").upper()
        except Exception:
            pass

    return (best_match["instrument_key"], expiry_code)


def derivative_tradingsymbol_matches(symbol: str, tradingsymbol: str, instrument_type: str) -> bool:
    """Return true only for the exact underlying's derivative trading symbol.

    Upstox's master contains lookalikes such as NIFTYNXT50..., which should not
    be treated as NIFTY contracts.  Current derivative symbols place the expiry
    immediately after the underlying, and that expiry starts with a digit.
    """
    symbol = symbol.upper()
    tradingsymbol = tradingsymbol.upper()
    if instrument_type == "OPT":
        return re.match(rf"^{re.escape(symbol)}\d.*\d{{4,5}}(?:CE|PE)$", tradingsymbol) is not None
    if instrument_type == "FUT":
        return re.match(rf"^{re.escape(symbol)}\d.*FUT$", tradingsymbol) is not None
    return tradingsymbol.startswith(symbol)


def generate_pcr_keys(
    base_strike: int,
    wing_count: int,
    strike_step: int,
    expiry_code: str,
) -> tuple[dict[str, list[str]], tuple[int, ...]]:
    strikes = tuple(
        base_strike + (offset * strike_step) for offset in range(-wing_count, wing_count + 1)
    )
    ce_keys = [f"NSE_FO|NIFTY{expiry_code}{strike}CE" for strike in strikes]
    pe_keys = [f"NSE_FO|NIFTY{expiry_code}{strike}PE" for strike in strikes]
    return {"CE": ce_keys, "PE": pe_keys}, strikes


# Initial PCR setup (will be updated dynamically)
PCR_KEYS, PCR_STRIKES = generate_pcr_keys(
    PCR_BASE_STRIKE,
    PCR_WING_COUNT,
    PCR_STRIKE_STEP,
    PCR_EXPIRY_CODE,
)

# MACRO_SYMBOLS moved to core/config.py (closure-captured by MacroState default_factory).

# SIGNAL_* name strings, SIGNAL_CONFIRMED_SET, SIGNAL_ABBREVIATIONS,
# REVIEW_ENTRY_*, and UPSTOX_CURRENCY_UNDERLYINGS now live in core/config.py
# and are re-exported into this module's namespace by `from core.config import *`
# at the top of the file.
PCR_KEY_SIDE = {key: "CE" for key in PCR_KEYS["CE"]} | {
    key: "PE" for key in PCR_KEYS["PE"]
}
PCR_KEY_STRIKE = {
    key: strike for key, strike in zip(PCR_KEYS["CE"], PCR_STRIKES, strict=True)
} | {
    key: strike for key, strike in zip(PCR_KEYS["PE"], PCR_STRIKES, strict=True)
}
SUBSCRIPTION_KEYS = list(dict.fromkeys(INSTRUMENT_KEYS + list(PCR_KEY_SIDE)))

# --- PHASE 2: Dynamic Strike Tracking ---
currently_tracked_strikes = set()

# LOG_STOP sentinel + log-path helpers + disk_writer_task live in gammaleak_runtime.io_logs;
# they're re-exported below (after `console` is created, since the writer logs through it).

# --------------------------- ASYNC TASKS & PIPELINE ---------------------------

async def sync_atm_window(websocket, token_to_instrument):
    global currently_tracked_strikes, PCR_KEY_SIDE, PCR_KEY_STRIKE
    while True:
        # 1. Get current Nifty LTP
        nifty_state = symbol_states.get("NSE_INDEX|Nifty 50")
        if not nifty_state or nifty_state.ltp <= 0:
            await asyncio.sleep(10)
            continue

        # 2. Calculate ATM window (±100 points)
        atm = int(round(nifty_state.ltp / 50.0) * 50)
        strikes = [atm-100, atm-50, atm, atm+50, atm+100]

        # 3. Resolve to numeric tokens and update dictionaries
        new_resolved_keys = set()
        for s in strikes:
            for side_tag in ("CE", "PE"):
                sym_key = f"NSE_FO|NIFTY{PCR_EXPIRY_CODE}{s}{side_tag}"
                resolved = resolve_key_from_master(sym_key)
                if not resolved:
                    continue
                new_resolved_keys.add(resolved)
                token_to_instrument[resolved] = sym_key
                if sym_key not in PCR_KEY_SIDE:
                    PCR_KEY_SIDE[sym_key] = side_tag
                    PCR_KEY_STRIKE[sym_key] = s

        # 4. Determine delta (what to sub/unsub) using resolved keys
        to_sub = list(new_resolved_keys - currently_tracked_strikes)
        to_unsub = list(currently_tracked_strikes - new_resolved_keys)

        if to_unsub:
            await websocket.send(json.dumps({
                "guid": "gammaleak-radar",
                "method": "unsub",
                "data": {"instrumentKeys": to_unsub}
            }))

        if to_sub:
            await websocket.send(json.dumps({
                "guid": "gammaleak-radar",
                "method": "sub",
                "data": {"mode": "full", "instrumentKeys": to_sub}
            }))

        currently_tracked_strikes = new_resolved_keys
        await asyncio.sleep(60) # Re-check every minute


def _poll_usdinr_bootstrap(headers: dict) -> None:
    """HTTP fallback for USDINR — polls until WebSocket delivers the first tick."""
    import requests as _req

    usdinr_key = next(iter(USDINR_KEYS), None)
    if not usdinr_key or usdinr_key not in symbol_states:
        return
    state = symbol_states[usdinr_key]
    if state.ltp != 0.0:
        return  # WebSocket already delivering — no need

    encoded = quote(usdinr_key, safe="")
    url = f"https://api.upstox.com/v2/market-quote/ltp?instrument_key={encoded}"
    try:
        resp = _req.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            payload = resp.json()
            # Upstox returns key with ":" instead of "|"
            api_key = usdinr_key.replace("|", ":")
            ltp = payload.get("data", {}).get(api_key, {}).get("last_price", 0)
            if ltp and state.ltp == 0.0:  # double-check WS hasn't arrived
                state.ltp = ltp
                if state.session_open is None:
                    state.session_open = ltp
                state.last_tick_ts = time.time()
                console.print(f"[green][OK] USDINR bootstrap via HTTP: {ltp:.4f}[/green]")
    except Exception:
        pass


def fetch_macro_worker():
    """USDINR HTTP bootstrap fallback — polls until WebSocket delivers first tick."""
    import time

    usdinr_bootstrapped = False

    while True:
        upstox_headers = {"Accept": "application/json", "Authorization": f"Bearer {ACCESS_TOKEN}"}

        # USDINR bootstrap fallback — poll until WS delivers first tick
        if not usdinr_bootstrapped:
            usdinr_key = next(iter(USDINR_KEYS), None)
            ws_alive = (
                usdinr_key
                and usdinr_key in symbol_states
                and symbol_states[usdinr_key].ltp != 0.0
            )
            if ws_alive:
                usdinr_bootstrapped = True
            else:
                _poll_usdinr_bootstrap(upstox_headers)

        if usdinr_bootstrapped:
            return  # Job done — no more polling needed

        time.sleep(5)


def split_upstox_instrument_key(instrument_key: str) -> tuple[str, str]:
    exchange, separator, symbol = instrument_key.partition("|")
    if not separator or not exchange or not symbol:
        raise ValueError(f"Invalid Upstox instrument key: {instrument_key}")
    return exchange.strip().upper(), symbol.strip().upper()


def instrument_key_uses_numeric_token(instrument_key: str) -> bool:
    _, symbol = split_upstox_instrument_key(instrument_key)
    return symbol.isdigit()


def historical_exchange_preferences(exchange: str, tradingsymbol: str) -> tuple[str, ...]:
    if tradingsymbol.startswith(UPSTOX_CURRENCY_UNDERLYINGS):
        if exchange == "NSE_FO":
            return ("NCD_FO", "NSE_FO", "BCD_FO")
        if exchange == "BSE_FO":
            return ("BCD_FO", "BSE_FO", "NCD_FO")
    if exchange == "MCX_FO":
        return ("MCX_FO", "NSE_COM")
    return (exchange,)


INSTRUMENT_MASTER_CACHE_PATH = Path("data") / "upstox_master_cache.csv.gz"


def _parse_instrument_master_bytes(payload: bytes) -> dict[str, list[dict[str, str]]]:
    """Parse the gz-encoded Upstox master into the symbol→rows index.

    `name` is captured alongside the other fields so callers can do exact
    underlying disambiguation (e.g. SENSEX vs SENSEX50 share a tradingsymbol
    prefix; only the `name` column reliably separates them).
    """
    try:
        decoded = gzip.decompress(payload)
    except (EOFError, OSError):
        decoded = payload
    reader = csv.DictReader(io.StringIO(decoded.decode("utf-8-sig")))
    master: dict[str, list[dict[str, str]]] = {}
    for row in reader:
        tradingsymbol = (row.get("tradingsymbol") or "").strip().upper()
        exchange = (row.get("exchange") or "").strip().upper()
        master_key = (row.get("instrument_key") or "").strip()
        expiry = (row.get("expiry") or "").strip()
        name = (row.get("name") or "").strip().upper()
        if not tradingsymbol or not exchange or not master_key:
            continue
        master.setdefault(tradingsymbol, []).append(
            {"exchange": exchange, "instrument_key": master_key, "expiry": expiry, "name": name}
        )
    return master


async def load_upstox_instrument_master(
    session: aiohttp.ClientSession,
) -> dict[str, list[dict[str, str]]]:
    """Load Upstox instrument master with disk-cache fallback.

    Priority:
      1. In-process memo (`_instrument_master_by_symbol`) — served instantly.
      2. Live HTTP fetch from Upstox CDN — always preferred when reachable,
         refreshes the disk cache on success.
      3. Disk cache (`data/upstox_master_cache.csv.gz`) — used only if the
         live fetch fails (transient network / Upstox outage). The cache is
         still useful even if days old: `get_active_expiry_key` filters
         `expiry > today` so stale expired contracts get dropped automatically
         and any live future-dated contract (MAY/JUN/JUL/...) resolves.
      4. If all three miss → raise. Callers must fail loudly; no hardcoded
         fallback to a dead contract.
    """
    global _instrument_master_by_symbol

    if _instrument_master_by_symbol is not None:
        return _instrument_master_by_symbol

    # 1. Try live fetch
    live_err: Exception | None = None
    try:
        async with session.get(UPSTOX_INSTRUMENT_MASTER_URL) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Instrument master HTTP {resp.status}: {body[:200]}")
            payload = await resp.read()
        master = _parse_instrument_master_bytes(payload)
        if not master:
            raise RuntimeError("Instrument master parsed empty — upstream format change?")
        _instrument_master_by_symbol = master
        # Refresh disk cache on every successful live fetch
        try:
            INSTRUMENT_MASTER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            INSTRUMENT_MASTER_CACHE_PATH.write_bytes(payload)
        except OSError as exc:
            console.print(f"[yellow]Master cache write failed ({exc}) — continuing with in-memory only[/yellow]")
        return master
    except Exception as exc:
        live_err = exc
        console.print(f"[yellow]Live master fetch failed: {exc} — trying disk cache[/yellow]")

    # 2. Fall back to disk cache
    if INSTRUMENT_MASTER_CACHE_PATH.exists():
        try:
            payload = INSTRUMENT_MASTER_CACHE_PATH.read_bytes()
            master = _parse_instrument_master_bytes(payload)
            if master:
                _instrument_master_by_symbol = master
                mtime = datetime.fromtimestamp(INSTRUMENT_MASTER_CACHE_PATH.stat().st_mtime, IST)
                console.print(f"[yellow]Using disk cache from {mtime:%Y-%m-%d %H:%M IST} — contracts are filtered by today's date so expired ones drop automatically[/yellow]")
                return master
        except Exception as exc:
            console.print(f"[red]Disk cache unreadable: {exc}[/red]")

    # 3. Both failed — refuse to boot with a stale guess
    raise RuntimeError(
        f"Cannot load Upstox instrument master (live: {live_err}; no usable disk cache at "
        f"{INSTRUMENT_MASTER_CACHE_PATH}). Fix network/check Upstox CDN and retry — refusing "
        f"to boot with a hardcoded dead contract."
    )


def resolve_key_from_master(instrument_key: str) -> str | None:
    """Resolve trading-symbol key to numeric token using already-loaded master (no HTTP)."""
    cached = _historical_key_cache.get(instrument_key)
    if cached:
        return cached
    if _instrument_master_by_symbol is None:
        return None
    try:
        exchange, tradingsymbol = split_upstox_instrument_key(instrument_key)
    except ValueError:
        return None
    matches = _instrument_master_by_symbol.get(tradingsymbol, [])
    for preferred_exchange in historical_exchange_preferences(exchange, tradingsymbol):
        for entry in matches:
            if entry["exchange"] == preferred_exchange:
                resolved = entry["instrument_key"]
                _historical_key_cache[instrument_key] = resolved
                return resolved
    if len(matches) == 1:
        resolved = matches[0]["instrument_key"]
        _historical_key_cache[instrument_key] = resolved
        return resolved
    return None


async def resolve_historical_instrument_key(
    session: aiohttp.ClientSession,
    instrument_key: str,
) -> str:
    cached_key = _historical_key_cache.get(instrument_key)
    if cached_key:
        return cached_key

    if instrument_key_uses_numeric_token(instrument_key):
        _historical_key_cache[instrument_key] = instrument_key
        return instrument_key

    # --- Bypass for Spot Indices (name-based keys accepted by Upstox API) ---
    # Both NSE_INDEX and BSE_INDEX use name-based keys (Nifty 50, India VIX,
    # SENSEX) which the Upstox REST API accepts as-is — no token swap needed.
    if instrument_key.startswith("NSE_INDEX|") or instrument_key.startswith("BSE_INDEX|"):
        _historical_key_cache[instrument_key] = instrument_key
        return instrument_key

    exchange, tradingsymbol = split_upstox_instrument_key(instrument_key)
    instrument_master = await load_upstox_instrument_master(session)
    matches = instrument_master.get(tradingsymbol, [])
    if not matches:
        raise RuntimeError(
            f"No instrument-master entry found for {instrument_key}. "
            f"Check whether the configured expiry symbol is still current."
        )

    for preferred_exchange in historical_exchange_preferences(exchange, tradingsymbol):
        for entry in matches:
            if entry["exchange"] == preferred_exchange:
                resolved_key = entry["instrument_key"]
                _historical_key_cache[instrument_key] = resolved_key
                return resolved_key

    if len(matches) == 1:
        resolved_key = matches[0]["instrument_key"]
        _historical_key_cache[instrument_key] = resolved_key
        return resolved_key

    candidates = ", ".join(
        f"{entry['exchange']} -> {entry['instrument_key']}" for entry in matches[:5]
    )
    raise RuntimeError(
        f"Unable to choose a historical instrument key for {instrument_key}. "
        f"Candidates from master: {candidates}"
    )


# Dataclasses (core/models) and shared state singletons (core/state) are
# already imported near the top of this file — moved up so SYMBOL_PROFILES /
# DEFAULT_SYMBOL_PROFILE / etc. could reference them at definition time.

# Engine-internal state with REBIND semantics — these stay module-local to
# GammaLeak.py because `global X; X = new_value` only updates the
# binding in this module's namespace; a copy in core/state.py would go stale.
_replay_mode: bool = False

# V5.0: FII/DII + Sonar global state
_fii_snapshot: "FIISnapshot | None" = None
_sonar_engine: "SonarNewsEngine | None" = None
_sonar_last_contexts: dict[str, "NewsContext"] = {}  # instrument -> latest NewsContext


# --------------------------- HELPERS ---------------------------


def looks_like_placeholder_token(token: str) -> bool:
    lowered = token.lower()
    return not token or "your_token" in lowered or "goes_here" in lowered


def is_token_expired(token: str) -> bool:
    import base64
    if not token or looks_like_placeholder_token(token):
        return True
    try:
        parts = token.split(".")
        if len(parts) != 3: return True
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp is None: return False
        return time.time() > exp
    except Exception:
        return False


def resolve_mock_mode() -> bool:
    override = os.environ.get("MOCK_MODE")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return looks_like_placeholder_token(ACCESS_TOKEN)


def get_display_name(instrument_key: str) -> str:
    return DISPLAY_NAMES.get(instrument_key, instrument_key.split("|")[-1])


from gammaleak_runtime.io_logs import (
    LOG_STOP,
    _safe_filename,
    append_csv_rows,
    append_event_row,
    disk_writer_task,
    get_events_log_path,
    get_log_dir,
    get_log_path,
    get_symbol_log_path,
)


def parse_review_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def is_after_review_start(ts_float: float) -> bool:
    dt = datetime.fromtimestamp(ts_float, IST)
    return (dt.hour, dt.minute) >= (OPENING_DISCOVERY_END_HOUR, OPENING_DISCOVERY_END_MINUTE)


def parse_replay_datetimes(values: pd.Series) -> pd.Series:
    cleaned = values.astype(str).str.strip()
    sample = cleaned[cleaned.ne("")].head(20)
    year_first = all(len(value) >= 10 and value[4] == "-" and value[7] == "-" for value in sample)

    if year_first:
        parsed = pd.to_datetime(cleaned, errors="coerce")
    else:
        parsed = pd.to_datetime(cleaned, dayfirst=True, errors="coerce")

    if parsed.isna().any():
        bad_values = cleaned[parsed.isna()].head(5).tolist()
        raise ValueError(
            "Replay datetime parse failed for "
            f"{int(parsed.isna().sum())} row(s). Sample bad values: {bad_values}"
        )
    return parsed


def get_symbol_profile(instrument_key: str) -> dict[str, float]:
    return SYMBOL_PROFILES.get(instrument_key, DEFAULT_SYMBOL_PROFILE)


def is_at_or_after(now_ist: datetime, hour: int, minute: int) -> bool:
    return (now_ist.hour, now_ist.minute) >= (hour, minute)


def is_before(now_ist: datetime, hour: int, minute: int) -> bool:
    return (now_ist.hour, now_ist.minute) < (hour, minute)


def get_session_badge(now_ist: datetime) -> str:
    if is_before(now_ist, SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE):
        return "Pre-open (no signals)"
    if is_before(now_ist, WARMUP_HOUR, WARMUP_MINUTE):
        return "Warmup (collecting ticks)"
    if is_before(now_ist, OPENING_DISCOVERY_END_HOUR, OPENING_DISCOVERY_END_MINUTE):
        return "Opening Range (stretch only)"
    if is_before(now_ist, EUROPE_WATCH_HOUR, EUROPE_WATCH_MINUTE):
        return "LIVE"
    return "Europe Watch"


def is_opening_discovery(now_ist: datetime) -> bool:
    return (
        is_at_or_after(now_ist, SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE)
        and is_before(now_ist, OPENING_DISCOVERY_END_HOUR, OPENING_DISCOVERY_END_MINUTE)
    )


def is_opening_range_window(now_ist: datetime) -> bool:
    return (
        is_at_or_after(now_ist, SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE)
        and is_before(now_ist, OPENING_RANGE_END_HOUR, OPENING_RANGE_END_MINUTE)
    )


def is_session_started(now_ist: datetime) -> bool:
    return is_at_or_after(now_ist, SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE)


def reset_state_for_new_session(state: SymbolState, trading_day: date) -> None:
    state.ticks = deque(maxlen=DEQUE_MAXLEN)
    state._timestamps.fill(0)
    state._prices.fill(0)
    state._tick_count = 0

    state.cum_price_volume = 0.0
    state.cum_volume = 0
    state.last_vtt = 0

    # Flow / CVD state — must reset on rollover or block-trade drift carries over
    state.cvd = 0
    state._prev_ltp_for_aggressor = 0.0
    state._prev_vtt_for_aggressor = 0
    state._last_aggressor = ""
    state.current_minute_epoch = -1
    state.minute_buy_vol = 0
    state.minute_sell_vol = 0
    state.last_completed_minute_buy = 0
    state.last_completed_minute_sell = 0
    state.last_completed_minute_delta = 0
    state.recent_minute_deltas.clear()
    state.session_high_tracked = 0.0
    state.session_low_tracked = 0.0
    state.cvd_at_session_high = 0
    state.cvd_at_session_low = 0
    state.high_pullback_seen = False
    state.low_pullback_seen = False
    state.divergence_label = ""
    state.divergence_ts = 0.0

    state.vwap = 0.0
    state.std_dev = 0.0
    state.z_score = 0.0
    state.ltp = 0.0
    state.ltp_style = "white"
    state.action_signal = SIGNAL_WARMING_UP
    state.action_style = "dim white"

    state.sig_state = 0
    state.alert_side = 0
    state.peak_z = 0.0
    state.prev_z = 0.0

    state.efficiency_ratio = 0.0
    state.hurst = 0.5
    state.regime = "NORMAL"

    state.session_day = trading_day
    state.session_open = None
    state.day_high = None
    state.day_low = None
    state.or_high = None
    state.or_low = None
    state.or_finalized = False
    state.was_outside_or = False
    state.is_fakeout = False
    state.last_tick_ts = None

    state.thesis_started_at = None
    state.thesis_age_secs = 0.0
    state.thesis_decay = False
    state.regime_shift_alert = False

    state.last_hurst_calc_ts = None
    state.ticks_since_hurst = 0
    
    state.last_vwap_touch_ts = None
    state.last_vwap_touch_side = 0
    state.vwap_rejection_active = False
    state.last_signal_exit_ts = None
    state.last_alert_fire_ts = None
    state.last_alert_fire_side = 0
    state.conviction_score = 0
    state.setup_label = ""
    state.event_blackout_reason = ""
    state.vwap_slope = 0.0
    state.price_slope_5m = 0.0

    # V4.0 reset
    state.atr = 0.0
    state.atr_session_mean = 0.0
    state.atr_ratio = 1.0
    state._ohlc_buckets = deque(maxlen=ATR_MAX_BUCKETS)
    state._current_bucket_ts = 0.0
    state._current_bucket = {"o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0}
    state._atr_sum = 0.0
    state._atr_count = 0
    state.dynamic_regime = ""
    state.implied_upper = 0.0
    state.implied_lower = 0.0
    state.straddle_premium = 0.0
    state.anchor_gate = ""
    state.oi_roc_ce_atm = 0.0
    state.oi_roc_pe_atm = 0.0


def ensure_session_rollover(state: SymbolState, tick_ist: datetime) -> None:
    trading_day = tick_ist.date()
    if state.session_day != trading_day:
        reset_state_for_new_session(state, trading_day)


def update_structure_state(state: SymbolState, ltp: float, tick_ist: datetime) -> None:
    if is_session_started(tick_ist) and state.session_open is None:
        state.session_open = ltp

    if is_session_started(tick_ist):
        state.day_high = ltp if state.day_high is None else max(state.day_high, ltp)
        state.day_low = ltp if state.day_low is None else min(state.day_low, ltp)

        if is_opening_range_window(tick_ist):
            state.or_high = ltp if state.or_high is None else max(state.or_high, ltp)
            state.or_low = ltp if state.or_low is None else min(state.or_low, ltp)
        elif is_at_or_after(tick_ist, OPENING_RANGE_END_HOUR, OPENING_RANGE_END_MINUTE):
            state.or_finalized = True

    if (
        not state.or_finalized
        or state.or_high is None
        or state.or_low is None
    ):
        state.was_outside_or = False
        state.is_fakeout = False
        state.ltp_style = "white"
        return

    inside_or = state.or_low <= ltp <= state.or_high
    if inside_or:
        if state.was_outside_or:
            state.is_fakeout = True
        state.was_outside_or = False
        state.ltp_style = "orange1"
    else:
        state.was_outside_or = True
        state.is_fakeout = False
        state.ltp_style = "bold magenta"


def should_recompute_hurst(state: SymbolState, timestamp: float) -> bool:
    if not HURST_ENABLED:
        return False
    # V3.0: Suspend Hurst during high-TPS stress
    nifty_state = symbol_states.get("NSE_INDEX|Nifty 50")
    if nifty_state and nifty_state.tps > TPS_HURST_SUSPEND_THRESHOLD:
        return False
    if state.last_hurst_calc_ts is None:
        return True
    if _replay_mode:
        return state.ticks_since_hurst >= HURST_REPLAY_RECALC_TICKS
    return (timestamp - state.last_hurst_calc_ts) >= HURST_LIVE_RECALC_SECS


def update_hurst_if_due(state: SymbolState, timestamp: float) -> None:
    state.ticks_since_hurst += 1
    if not should_recompute_hurst(state, timestamp):
        return

    hurst_window_secs = REPLAY_ROLLING_WINDOW_SECS if _replay_mode else HURST_LIVE_WINDOW_SECS
    hurst_prices = get_recent_prices_ordered(state, timestamp, hurst_window_secs)
    state.hurst = compute_hurst(hurst_prices)
    state.last_hurst_calc_ts = timestamp
    state.ticks_since_hurst = 0


def get_reference_time_ist() -> datetime:
    latest_tick = max(
        (state.last_tick_ts for state in symbol_states.values() if state.last_tick_ts is not None),
        default=None,
    )
    if latest_tick is None:
        return datetime.now(IST)
    return datetime.fromtimestamp(latest_tick, IST)


def reset_runtime_state() -> None:
    symbol_states.clear()
    for key in INSTRUMENT_KEYS:
        symbol_states[key] = SymbolState()

    macro_state.quotes = {label: MacroQuote() for label in MACRO_SYMBOLS}
    pcr_state.ce_oi.clear()
    pcr_state.pe_oi.clear()
    pcr_state.last_updated = None
    pcr_state.iv_history.clear()
    pcr_state.gamma_history.clear()
    pcr_state.tbq_by_strike.clear()
    pcr_state.tsq_by_strike.clear()


def _active_rolling_window() -> int:
    return REPLAY_ROLLING_WINDOW_SECS if _replay_mode else ROLLING_WINDOW_SECS


def get_recent_prices_ordered(
    state: SymbolState, now: float, lookback_secs: int | None = None
) -> np.ndarray:
    n = min(state._tick_count, DEQUE_MAXLEN)
    if n == 0:
        return np.array([], dtype=np.float64)

    if n < DEQUE_MAXLEN:
        timestamps = state._timestamps[:n]
        prices = state._prices[:n]
    else:
        start = state._tick_count % DEQUE_MAXLEN
        timestamps = np.concatenate((state._timestamps[start:], state._timestamps[:start]))
        prices = np.concatenate((state._prices[start:], state._prices[:start]))

    cutoff = now - (lookback_secs if lookback_secs is not None else _active_rolling_window())
    return prices[timestamps >= cutoff]


def update_vwap(state: SymbolState, ltp: float, cumulative_volume: int) -> None:
    # --- TWAP Fallback for Spot Indices (Zero Volume) ---
    if cumulative_volume == 0:
        state.cum_price_volume += ltp
        state.cum_volume += 1
    else:
        # Standard VWAP for Futures/Equities
        incremental_vol = cumulative_volume - state.last_vtt
        if incremental_vol < 0:
            incremental_vol = cumulative_volume
        state.last_vtt = cumulative_volume

        if incremental_vol > 0:
            state.cum_price_volume += ltp * incremental_vol
            state.cum_volume += incremental_vol

    # Prevent division by zero
    if state.cum_volume > 0:
        state.vwap = state.cum_price_volume / state.cum_volume


def update_rolling_stddev(state: SymbolState, now: float) -> None:
    recent_prices = get_recent_prices_ordered(state, now)
    if recent_prices.shape[0] < 2:
        state.std_dev = 0.0
        return

    raw_sd = float(np.std(recent_prices))

    # Crash guard: if the 60-sec SD is extremely inflated (crash/spike inside
    # the window), blend with a 5-min SD so that the Z-score isn't completely
    # crushed.  The 5-min window provides a more stable baseline.
    long_prices = get_recent_prices_ordered(state, now, lookback_secs=SD_FLOOR_LONGWIN_SECS)
    if long_prices.shape[0] >= 2:
        long_sd = float(np.std(long_prices))
        # If 60-sec SD is more than 3× the 5-min SD, the short window is
        # dominated by a single violent move — cap it to dampen the spike.
        if long_sd > 0 and raw_sd > long_sd * 3:
            raw_sd = long_sd * 3

    # --- ATR-based SD floor (fixes the timescale mismatch) ---
    # Problem: session VWAP drifts away from LTP over hours, but 60-sec SD
    # only captures micro-noise. Z = (LTP-VWAP)/SD_60s explodes to ±10 cap
    # after ~20 minutes, making every signal meaningless.
    # Fix: floor SD at 40% of session ATR so Z stays in a usable range.
    # ATR (14-bar, 1-min) captures the actual scale of intraday movement.
    sd_floor = SD_MIN_ABSOLUTE
    if state.atr > 0:
        # Primary floor: fraction of ATR (available after ~15 min of data)
        sd_floor = max(sd_floor, state.atr * SD_FLOOR_ATR_MULTIPLIER)
    elif long_prices.shape[0] >= 2:
        # Early-session fallback: use 5-min SD as floor before ATR is ready
        sd_floor = max(sd_floor, long_sd * 0.5)

    # Legacy micro-floor from 60-sec price range (still useful for flat markets)
    price_range = float(recent_prices.max() - recent_prices.min())
    micro_floor = price_range * SD_FLOOR_ATR_FRACTION

    state.std_dev = max(raw_sd, sd_floor, micro_floor)


def update_zscore(state: SymbolState) -> None:
    if state.std_dev == 0.0:
        state.z_score = 0.0
    else:
        raw_z = (state.ltp - state.vwap) / state.std_dev
        state.z_score = float(np.clip(raw_z, -Z_SCORE_CAP, Z_SCORE_CAP))


# ================================================================
# V5.2 Micro-Structural Layer — Layer 1/2/3 per-symbol updates
# Pure computation, no interaction with sig_state machine.
# ================================================================

# V5.2 micro-structural momentum updaters (update_micro_z, update_z_velocity,
# update_amber_state, update_tick_rate) moved to signals/momentum.py.
from signals.momentum import (  # noqa: F401
    update_micro_z,
    update_z_velocity,
    update_amber_state,
    update_tick_rate,
)


from analytics.math_stats import (
    HURST_ENABLED,
    HURST_MIN_POINTS,
    compute_efficiency_ratio,
    compute_hurst,
)


def usdinr_z_multiplier(instrument_key: str, ltp: float) -> float:
    if instrument_key not in USDINR_KEYS:
        return 1.0

    near_rbi_level = any(
        abs(ltp - level) <= RBI_PROXIMITY for level in RBI_INTERVENTION_LEVELS
    )
    return USDINR_Z_BOOST if near_rbi_level else 1.0


# determine_confirmed_signal + reset_thesis_state + update_thesis_state moved
# to signals/exhaustion.py (signal-conviction-lifecycle module).
from signals.exhaustion import (  # noqa: F401
    determine_confirmed_signal,
    reset_thesis_state,
    update_thesis_state,
)


def is_outside_opening_range(state: SymbolState) -> bool:
    if state.or_high is None or state.or_low is None or not state.or_finalized:
        return False
    return not (state.or_low <= state.ltp <= state.or_high)


# --- PHASE 3: VWAP Rejection Memory ---
def update_vwap_rejection(state: SymbolState, now: float) -> None:
    abs_z = abs(state.z_score)
    z_side = 1 if state.z_score > 0 else -1 if state.z_score < 0 else 0

    if abs_z < VWAP_TOUCH_Z_THRESHOLD:
        # Near VWAP: check for side-change BEFORE overwriting the side
        if state.vwap_rejection_active and z_side != 0 and z_side != state.last_vwap_touch_side:
            state.vwap_rejection_active = False
            state.last_vwap_touch_ts = None
        # Now update the touch record
        state.last_vwap_touch_ts = now
        state.last_vwap_touch_side = z_side
    else:
        # Away from VWAP: activate rejection only once (don't reset timestamp)
        if not state.vwap_rejection_active and state.last_vwap_touch_ts is not None and z_side == state.last_vwap_touch_side:
            state.vwap_rejection_active = True

    # Cooldown: use the original touch timestamp (not constantly reset)
    if (state.vwap_rejection_active and state.last_vwap_touch_ts is not None
        and (now - state.last_vwap_touch_ts) > VWAP_REJECTION_COOLDOWN_SECS):
        state.vwap_rejection_active = False
        state.last_vwap_touch_ts = None


def check_signal_cooldown(state: SymbolState, now: float) -> bool:
    if state.last_signal_exit_ts is None:
        return False
    return (now - state.last_signal_exit_ts) < SIGNAL_COOLDOWN_SECS


def check_same_side_cooldown(state: SymbolState, now: float, side: int) -> bool:
    """True if a same-side alert fired within the per-alert cooldown window.
    Opposite-side alerts bypass — a genuine flip is a new regime, not noise."""
    if state.last_alert_fire_ts is None or state.last_alert_fire_side == 0:
        return False
    if side != state.last_alert_fire_side:
        return False
    return (now - state.last_alert_fire_ts) < SAME_SIDE_ALERT_COOLDOWN_SECS


# --- Event calendar cache (parsed once, refreshed only on file mtime change) ---
_event_cache: dict = {"mtime": None, "events": []}


def _load_event_calendar() -> list[tuple[datetime, str]]:
    """Return list of (datetime_ist, label) parsed from SCHEDULED_EVENTS plus
    any entries in data/events.txt (ISO8601,label per line)."""
    events: list[tuple[datetime, str]] = []
    for iso_ts, label in SCHEDULED_EVENTS:
        try:
            dt = datetime.fromisoformat(iso_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            events.append((dt.astimezone(IST), label))
        except Exception:
            continue

    try:
        import os
        if os.path.exists(EVENT_FILE_PATH):
            mtime = os.path.getmtime(EVENT_FILE_PATH)
            if _event_cache["mtime"] != mtime:
                extra: list[tuple[datetime, str]] = []
                with open(EVENT_FILE_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(",", 1)
                        if len(parts) != 2:
                            continue
                        try:
                            dt = datetime.fromisoformat(parts[0].strip())
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=IST)
                            extra.append((dt.astimezone(IST), parts[1].strip()))
                        except Exception:
                            continue
                _event_cache["mtime"] = mtime
                _event_cache["events"] = extra
            events.extend(_event_cache["events"])
    except Exception:
        pass
    return events


def check_event_blackout(now_ist: datetime) -> str:
    """Return blackout reason (non-empty) if we're inside a blackout window.
    Covers (a) scheduled macro events ±15min, (b) weekly Thu expiry pin 15:00–15:30."""
    # Weekly expiry pin
    if now_ist.weekday() == EXPIRY_BLACKOUT_WEEKDAY:
        start_h, start_m = EXPIRY_BLACKOUT_START
        end_h, end_m = EXPIRY_BLACKOUT_END
        cur_mins = now_ist.hour * 60 + now_ist.minute
        if start_h * 60 + start_m <= cur_mins < end_h * 60 + end_m:
            return "EXPIRY PIN"

    # Scheduled macro events
    for dt, label in _load_event_calendar():
        pre = dt - timedelta(minutes=EVENT_BLACKOUT_PRE_MINS)
        post = dt + timedelta(minutes=EVENT_BLACKOUT_POST_MINS)
        if pre <= now_ist <= post:
            return label
    return ""


def compute_conviction_score(
    state: SymbolState,
    instrument_key: str,
    side: int,
    abs_z: float,
    alert_z: float,
    exhaustion_peak: float,
    er_threshold: float,
    hurst_threshold: float,
    drift_dir: int,
) -> int:
    """1–5 conviction score for a fade signal.
    +1 base, +1 exhaustion-grade Z, +1 regime supports fade (not trending),
    +1 drift aligns with fade direction (side opposes drift), +1 OI flow alignment."""
    score = 1
    if abs_z >= exhaustion_peak:
        score += 1
    # Fade plays work best when regime is NOT trending
    if state.efficiency_ratio < er_threshold and state.hurst < hurst_threshold:
        score += 1
    # Drift aligned with fade direction: fade side = -Z-side, so fade is "against stretch"
    # The fade trade is: side == -alert_side (we fade the +Z stretch by going SHORT = side -1)
    # drift_dir aligning with FADE direction means drift_dir == -side
    if drift_dir != 0 and side != 0 and drift_dir == -side:
        score += 1
    # OI flow alignment (only meaningful for index symbols with OI data)
    flow = state.oi_flow_label or ""
    if side > 0 and flow in ("NEW SHORTS", "LONG EXIT"):
        # Fade SHORT on positive Z: want writers/shorts building
        score += 1
    elif side < 0 and flow in ("NEW LONGS", "SHORT COVER"):
        # Fade LONG on negative Z: want longs/covering building
        score += 1
    return min(5, max(1, score))


def classify_setup_label(
    state: SymbolState,
    side: int,
    peak_z: float,
    exhaustion_peak: float,
    conviction: int,
    now_ist: datetime,
) -> str:
    """Pick a named setup based on current context. side = Z-direction (stretch side).
    The fade trade direction is -side (long when Z negative, short when Z positive)."""
    # Expiry pin session on Thursday afternoon
    if now_ist.weekday() == EXPIRY_BLACKOUT_WEEKDAY and now_ist.hour >= 14:
        return SETUP_EXPIRY_PIN

    abs_peak = abs(peak_z)
    # VWAP reclaim — a stretch into VWAP from wrong side
    if state.last_vwap_touch_side != 0 and state.vwap_rejection_active:
        return SETUP_VWAP_RECLAIM_L if side < 0 else SETUP_VWAP_RECLAIM_S

    # Opening-range break context
    if state.or_finalized and is_outside_opening_range(state):
        return SETUP_ORB_BREAK_L if side < 0 else SETUP_ORB_BREAK_S

    # Exhaustion-grade
    if abs_peak >= exhaustion_peak:
        return SETUP_EXHAUSTION_REV_L if side > 0 else SETUP_EXHAUSTION_REV_S

    # Standard fade — label by conviction tier
    if conviction >= 4:
        return SETUP_FADE_HIGH_L if side > 0 else SETUP_FADE_HIGH_S
    return SETUP_FADE_LOW_L if side > 0 else SETUP_FADE_LOW_S


def compute_vwap_slope(state: SymbolState, now: float) -> float:
    recent_prices = get_recent_prices_ordered(state, now)
    if recent_prices.shape[0] < 3:
        return 0.0
    try:
        x = np.arange(len(recent_prices), dtype=np.float64)
        coeffs = np.polyfit(x, recent_prices, 1)
        return float(coeffs[0])
    except:
        return 0.0


def compute_price_slope_5m(state: SymbolState, now: float) -> float:
    """5-minute price slope for drift detection. More stable than the 60-sec
    vwap_slope — captures genuine directional drift vs micro-noise."""
    prices = get_recent_prices_ordered(state, now, lookback_secs=300)
    if prices.shape[0] < 10:
        return 0.0
    try:
        x = np.arange(len(prices), dtype=np.float64)
        return float(np.polyfit(x, prices, 1)[0])
    except:
        return 0.0


def get_drift_direction(state: SymbolState) -> int:
    """Detect market drift from 5-min price slope.
    Returns +1 (bullish drift), -1 (bearish drift), or 0 (no drift / sideways)."""
    if state.vwap <= 0:
        return 0
    threshold = state.vwap * 5e-6
    if state.price_slope_5m > threshold:
        return 1
    elif state.price_slope_5m < -threshold:
        return -1
    return 0


# --- PHASE 4: Cross-Asset Suppression ---
CROSS_ASSET_SUPPRESSION = {
    "MCX_FO|CRUDEOIL26MAYFUT": ["NSE_INDEX|Nifty 50", "NSE_EQ|RELIANCE"],
    "NSE_FO|USDINR26MAYFUT": ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"],
    "NSE_EQ|HDFCBANK": ["NSE_INDEX|Nifty Bank"],
}

def is_cross_asset_suppressed(instrument_key: str) -> bool:
    for driver_key, dependent_keys in CROSS_ASSET_SUPPRESSION.items():
        if instrument_key not in dependent_keys:
            continue
        driver_state = symbol_states.get(driver_key)
        if driver_state is None:
            continue
        if driver_state.efficiency_ratio >= ER_TREND_THRESHOLD and driver_state.hurst >= HURST_THRESHOLD:
            return True
    return False


# check_gamma_flush moved to orderflow/gamma.py.
from orderflow.gamma import check_gamma_flush  # noqa: F401


# ========================= V4.0: ADAPTIVE REGIME ENGINE =========================


def update_ohlc_bucket(state: SymbolState, ltp: float, timestamp: float) -> None:
    """Aggregate raw ticks into 1-minute OHLC buckets for ATR calculation."""
    bucket_start = timestamp - (timestamp % ATR_BUCKET_SECS)

    if state._current_bucket_ts == 0.0:
        # First tick — initialize bucket
        state._current_bucket_ts = bucket_start
        state._current_bucket = {"o": ltp, "h": ltp, "l": ltp, "c": ltp}
        return

    if bucket_start == state._current_bucket_ts:
        # Same bucket — update H/L/C
        b = state._current_bucket
        b["h"] = max(b["h"], ltp)
        b["l"] = min(b["l"], ltp)
        b["c"] = ltp
    else:
        # New bucket — finalize previous, start new
        state._ohlc_buckets.append(state._current_bucket.copy())
        state._current_bucket_ts = bucket_start
        state._current_bucket = {"o": ltp, "h": ltp, "l": ltp, "c": ltp}


def compute_atr_from_buckets(state: SymbolState) -> float:
    """Compute ATR from completed 1-minute OHLC buckets. Returns 0 if insufficient data."""
    buckets = state._ohlc_buckets
    n = len(buckets)
    if n < ATR_PERIOD + 1:
        return 0.0

    # Use the last ATR_PERIOD+1 buckets
    recent = list(buckets)[-ATR_PERIOD - 1:]
    tr_values = []
    for i in range(1, len(recent)):
        curr = recent[i]
        prev_close = recent[i - 1]["c"]
        tr = max(
            curr["h"] - curr["l"],
            abs(curr["h"] - prev_close),
            abs(curr["l"] - prev_close),
        )
        tr_values.append(tr)

    atr = sum(tr_values) / len(tr_values)

    # Update session running mean
    state._atr_sum += atr
    state._atr_count += 1
    state.atr_session_mean = state._atr_sum / state._atr_count if state._atr_count > 0 else atr

    # ATR ratio: how volatile is current ATR vs session average
    state.atr_ratio = atr / state.atr_session_mean if state.atr_session_mean > 0 else 1.0
    state.atr_ratio = max(ATR_Z_SCALE_FLOOR, min(ATR_Z_SCALE_CAP, state.atr_ratio))

    state.atr = atr
    return atr


# compute_oi_roc + classify_dynamic_regime moved to signals/regimes.py.
# classify_oi_flow + record_oi_flow_sample + _oi_flow_last_sample_ts moved to
# orderflow/oi_flow.py. Both star-imported here so existing call sites
# (`classify_dynamic_regime(state, ts)`, `classify_oi_flow(nifty_state)`, etc.)
# resolve unchanged. signals.regimes already imports the orderflow names; the
# explicit import below mirrors them into GammaLeak's namespace too.
from orderflow.oi_flow import *  # noqa: F401, F403
from signals.regimes import *  # noqa: F401, F403


def update_oi_roc_tracking(instrument_key: str, oi: float, timestamp: float) -> None:
    """Push OI into per-strike rolling history for RoC calculation."""
    if instrument_key not in PCR_KEY_SIDE or instrument_key not in PCR_KEY_STRIKE:
        return
    strike = PCR_KEY_STRIKE[instrument_key]
    side = PCR_KEY_SIDE[instrument_key]

    if side == "CE":
        if strike not in pcr_state.oi_history_ce:
            pcr_state.oi_history_ce[strike] = deque(maxlen=OI_ROC_HISTORY_MAXLEN)
        pcr_state.oi_history_ce[strike].append((timestamp, oi))
    else:
        if strike not in pcr_state.oi_history_pe:
            pcr_state.oi_history_pe[strike] = deque(maxlen=OI_ROC_HISTORY_MAXLEN)
        pcr_state.oi_history_pe[strike].append((timestamp, oi))


def update_option_ltp(instrument_key: str, ltp: float) -> None:
    """Store latest option LTP per strike for straddle box calculation."""
    if instrument_key not in PCR_KEY_SIDE or instrument_key not in PCR_KEY_STRIKE:
        return
    strike = PCR_KEY_STRIKE[instrument_key]
    side = PCR_KEY_SIDE[instrument_key]
    if side == "CE":
        pcr_state.ce_ltp_by_strike[strike] = ltp
    else:
        pcr_state.pe_ltp_by_strike[strike] = ltp


def compute_straddle_box(state: SymbolState) -> tuple[float, float]:
    """Calculate implied upper/lower range from ATM straddle premium + VWAP anchor.
    Returns (implied_upper, implied_lower). Falls back to (0, 0) if data missing."""
    if not STRADDLE_BOX_ENABLED or state.ltp <= 0 or state.vwap <= 0:
        return 0.0, 0.0

    atm = int(round(state.ltp / 50.0) * 50)
    ce_ltp = pcr_state.ce_ltp_by_strike.get(atm, 0.0)
    pe_ltp = pcr_state.pe_ltp_by_strike.get(atm, 0.0)

    if ce_ltp <= 0 or pe_ltp <= 0:
        return 0.0, 0.0

    premium = ce_ltp + pe_ltp
    state.straddle_premium = premium
    state.implied_upper = state.vwap + (premium / 2)
    state.implied_lower = state.vwap - (premium / 2)
    return state.implied_upper, state.implied_lower


def check_anchor_alignment(side: int) -> tuple[bool, str]:
    """Bidirectional cross-asset gate for Nifty signals.
    - Shorts (side<0): permitted only when BN AND REL Z < -0.5
    - Longs  (side>0): permitted only when BN AND REL Z > +0.5
    Returns (permitted, reason)."""
    bn_state = symbol_states.get("NSE_INDEX|Nifty Bank")
    rel_state = symbol_states.get("NSE_EQ|RELIANCE")

    if bn_state is None or rel_state is None:
        return True, ""  # If anchors not tracked, don't block

    if side < 0:
        # SHORT gate: both anchors must be dragging down
        bn_ok = bn_state.z_score < CROSS_DIVERGENCE_Z_THRESHOLD
        rel_ok = rel_state.z_score < CROSS_DIVERGENCE_Z_THRESHOLD
    elif side > 0:
        # LONG gate: both anchors must be lifting up
        bn_ok = bn_state.z_score > -CROSS_DIVERGENCE_Z_THRESHOLD   # +0.5
        rel_ok = rel_state.z_score > -CROSS_DIVERGENCE_Z_THRESHOLD
    else:
        return True, ""

    if bn_ok and rel_ok:
        return True, "ANCHORS ALIGNED"
    elif not bn_ok and not rel_ok:
        return False, "BN+REL DIVERGE"
    elif not bn_ok:
        return False, "BN DIVERGE"
    else:
        return False, "REL DIVERGE"


# classify_dynamic_regime moved to signals/regimes.py (imported above).


def get_adaptive_z_thresholds(
    state: SymbolState, base_alert_z: float, base_confirm_z: float, base_exit_z: float
) -> tuple[float, float, float]:
    """Scale Z-score thresholds based on ATR ratio AND India VIX level.

    Layer 1 — ATR ratio: widen in volatile sessions, tighten in quiet ones.
    Layer 2 — VIX regime: fear (VIX>18) widens further, complacency (VIX<12) tightens.

    Extreme-move bypass: if |Z| already exceeds 2× base alert, cap scale at 1.0
    so crashes/spikes are never suppressed."""
    if not ATR_Z_SCALE_ENABLED or state.atr_ratio == 0:
        # Even without ATR scaling, apply VIX layer
        v_scale = vix_z_scale()
        return base_alert_z * v_scale, base_confirm_z * v_scale, base_exit_z * v_scale

    # Extreme-move bypass: don't raise the bar when Z is already screaming
    if abs(state.z_score) >= base_alert_z * 2:
        atr_scale = min(state.atr_ratio, 1.0)
    else:
        atr_scale = state.atr_ratio

    # Layer VIX on top of ATR scaling
    v_scale = vix_z_scale()
    combined = atr_scale * v_scale

    return base_alert_z * combined, base_confirm_z * combined, base_exit_z * combined


# update_thesis_state moved to signals/exhaustion.py (imported above).


def _block_confirm_promotion(state: SymbolState, now_ts: float, reason: str) -> None:
    """Abort an alert when the regime gate rejects a 1→2 promotion.

    Mirrors the abs_z<exit_z reset path (sig_state=0, alert_side=0, peak_z=0,
    cleared conviction). The gate reason is stored in setup_label so the
    resulting ABORT row in events.csv is identifiable as a regime block
    (filter events.csv on setup_label LIKE 'REGIME_BLOCK:%').
    """
    state.sig_state = 0
    state.alert_side = 0
    state.peak_z = 0.0
    state.action_signal = f"REGIME_BLOCK {reason}"
    state.action_style = "dim magenta"
    state.last_signal_exit_ts = now_ts
    state.conviction_score = 0
    state.setup_label = f"REGIME_BLOCK:{reason}"


def update_signal_engine(
    instrument_key: str, state: SymbolState, now_ist: datetime | None = None
) -> None:
    # VIX is context-only — never generate trade signals for it
    if instrument_key == VIX_INSTRUMENT_KEY:
        state.action_signal = SIGNAL_NO_EDGE
        state.action_style = "dim white"
        state.regime = "CONTEXT"
        return

    if now_ist is None:
        now_ist = datetime.now(IST)

    # Snapshot pre-tick state for event emission. alert_side is captured because
    # ABORT (1→0) and EXIT (2→0) transitions zero it out before we'd record.
    prev_sig_state = state.sig_state
    prev_alert_side = state.alert_side

    profile = get_symbol_profile(instrument_key)
    base_alert_z = profile["alert_z"] * usdinr_z_multiplier(instrument_key, state.ltp)
    base_confirm_z = profile["confirm_z"] * usdinr_z_multiplier(instrument_key, state.ltp)
    base_exit_z = profile["exit_z"] * usdinr_z_multiplier(instrument_key, state.ltp)
    exhaustion_peak = profile["exhaustion_peak"]
    er_threshold = profile["er_trend_threshold"]
    hurst_threshold = profile["hurst_trend_threshold"]

    # V4.0: Scale Z thresholds by ATR ratio (volatile market = wider thresholds)
    alert_z, confirm_z, exit_z = get_adaptive_z_thresholds(
        state, base_alert_z, base_confirm_z, base_exit_z
    )

    if now_ist.hour < WARMUP_HOUR or (
        now_ist.hour == WARMUP_HOUR and now_ist.minute < WARMUP_MINUTE
    ):
        state.sig_state = 0
        state.alert_side = 0
        state.peak_z = 0.0
        state.regime = "WARMUP"
        state.dynamic_regime = ""
        state.action_signal = SIGNAL_WARMING_UP
        state.action_style = "dim white"
        reset_thesis_state(state)
        state.prev_z = state.z_score
        return

    # Institutional Phase 1: Event blackout — silence the engine ±15min around
    # scheduled macro events and inside the Thursday 15:00–15:30 expiry pin.
    blackout = check_event_blackout(now_ist)
    state.event_blackout_reason = blackout
    if blackout:
        state.sig_state = 0
        state.alert_side = 0
        state.peak_z = 0.0
        state.regime = f"BLACKOUT | {blackout}"
        state.action_signal = f"EVENT BLACKOUT | {blackout}"
        state.action_style = "yellow"
        state.setup_label = ""
        state.conviction_score = 0
        reset_thesis_state(state)
        state.prev_z = state.z_score
        if prev_sig_state != 0:
            try:
                append_event_row(
                    timestamp=state.last_tick_ts or time.time(),
                    symbol=get_display_name(instrument_key),
                    event_type=("EXIT" if prev_sig_state == 2 else "ABORT"),
                    side=prev_alert_side, z_score=state.z_score, ltp=state.ltp,
                    regime=state.regime, setup_label="", conviction=0,
                )
            except Exception:
                pass
        return

    # Per-symbol favorable-move threshold for the MFE-gated CONFIRM.
    # runtime_bar = max(MFE_ATR_K × state.atr, per-symbol floor)
    # On a volatile day state.atr scales the bar up; on a calm day or cold
    # start (atr=0), the floor dominates so noise can't cross by default.
    _display_for_mfe = get_display_name(instrument_key)
    _mfe_floor = MIN_FAVORABLE_POINTS_PER_SYMBOL.get(_display_for_mfe)
    if _mfe_floor is None:
        _mfe_floor = abs(state.ltp) * (REVIEW_DEFAULT_MIN_FAVORABLE_PCT / 100.0)
    _min_fav_pts = max(MFE_ATR_K * max(0.0, state.atr), float(_mfe_floor))

    # Update the running MFE for an active signal. Uses the entry price
    # captured at the 0→1 transition (or at a side flip during ALERT).
    if state.alert_entry_ltp > 0.0 and state.alert_side != 0:
        if state.alert_side > 0:        # fade short — favorable = price falling
            _favorable = state.alert_entry_ltp - state.ltp
        else:                           # fade long — favorable = price rising
            _favorable = state.ltp - state.alert_entry_ltp
        if _favorable > state.signal_mfe_points:
            state.signal_mfe_points = _favorable

    regime_tags: list[str] = []
    er_trending = state.efficiency_ratio >= er_threshold
    hurst_trending = HURST_ENABLED and state.hurst >= hurst_threshold
    if er_trending:
        regime_tags.append("ER TREND")
    if hurst_trending:
        regime_tags.append("HURST TREND")
    # Suppress fade signals when EITHER indicator flags trending.
    # Previously required AND, which let through fade-shorts on days where
    # Hurst flagged a clear trend but ER hadn't yet crossed its threshold
    # (chop within an uptrend). Empirically that produced losing fade-shorts
    # during sustained moves -- 2026-05-20 morning rally was the case study.
    # The downstream trend-follower (~line 1610) still requires BOTH ER >= 0.60
    # AND Hurst >= 0.55 to actually FIRE a momentum signal, so this only
    # widens the silence-zone, not the signal universe.
    regime_trending = er_trending or hurst_trending
    if usdinr_z_multiplier(instrument_key, state.ltp) != 1.0:
        regime_tags.append("RBI ZONE")

    # V4.0: Run adaptive regime classifier (only for Nifty spot)
    if instrument_key == "NSE_INDEX|Nifty 50":
        dyn_regime = classify_dynamic_regime(state, state.last_tick_ts or time.time())
        state.dynamic_regime = dyn_regime

        # Compute straddle-implied box
        compute_straddle_box(state)

        # Cross-asset anchor gate (side-aware, computed after Z-score)
        _anchor_side = 1 if state.z_score > 0 else -1 if state.z_score < 0 else 0
        anchor_ok, anchor_reason = check_anchor_alignment(_anchor_side)
        state.anchor_gate = anchor_reason

        if dyn_regime:
            regime_tags.append(dyn_regime)
        if not anchor_ok:
            regime_tags.append(anchor_reason)

    state.regime = " | ".join(regime_tags) if regime_tags else "NORMAL"

    z_score = state.z_score
    abs_z = abs(z_score)
    side = 1 if z_score > 0 else -1 if z_score < 0 else 0
    prev_abs_z = abs(state.prev_z)

    # V4.0: In EXPANSION regime, suppress fade signals and only allow momentum/trend
    if instrument_key == "NSE_INDEX|Nifty 50" and state.dynamic_regime == REGIME_EXPANSION:
        if state.sig_state != 2:
            regime_trending = True  # Force trend-following logic path

    # V4.0: In GAMMA SQUEEZE regime, the V3 gamma_flush_active flag takes priority
    # (handled at end of function). No extra intervention needed here.

    # V4.1: Bidirectional anchor gate — block new Nifty signals if anchors not aligned
    anchor_blocked = False
    if (instrument_key == "NSE_INDEX|Nifty 50"
            and state.anchor_gate and state.anchor_gate != "ANCHORS ALIGNED"
            and side != 0 and state.sig_state == 0):
        anchor_blocked = True

    if regime_trending and state.sig_state != 2:
        # Normalize slope relative to price level (0.001% of VWAP)
        slope_threshold = max(state.vwap, 1.0) * 1e-5
        has_momentum = (
            abs_z >= 3.0
            and state.efficiency_ratio >= 0.60
            and state.hurst >= 0.55
            and abs(state.vwap_slope) > slope_threshold
        )
        
        if has_momentum:
            if side > 0:
                state.action_signal = SIGNAL_MOMENTUM_LONG
                state.action_style = "green"
            elif side < 0:
                state.action_signal = SIGNAL_MOMENTUM_SHORT
                state.action_style = "red"
            else:
                state.action_signal = SIGNAL_TREND_STAND_DOWN
                state.action_style = "magenta"
        else:
            state.action_signal = SIGNAL_TREND_STAND_DOWN
            state.action_style = "magenta"
        
        state.sig_state = 0
        state.alert_side = 0
        state.peak_z = 0.0
        reset_thesis_state(state)
        state.prev_z = z_score
        return

    now_ts = state.last_tick_ts or time.time()
    update_vwap_rejection(state, now_ts)

    # V4.1: Drift detection — suppress fade signals that fight the drift
    drift_dir = get_drift_direction(state)
    # When Z-stretch ALIGNS with drift (both positive or both negative), the
    # fade trade would fight the drift direction.
    # e.g. bullish drift + positive Z → fade=SHORT fights drift → suppress
    # e.g. bearish drift + negative Z → fade=LONG fights drift → suppress
    fade_opposes_drift = drift_dir != 0 and side != 0 and side * drift_dir > 0

    if state.sig_state == 0:
        cross_suppressed = is_cross_asset_suppressed(instrument_key)
        cooldown_active = check_signal_cooldown(state, now_ts)
        same_side_blocked = check_same_side_cooldown(state, now_ts, side)
        # VWAP rejection: suppress repeated same-side signals after a VWAP touch,
        # BUT override the rejection when Z is extreme (crash/spike) — the move
        # is clearly decisive and shouldn't be blocked by a prior VWAP touch.
        vwap_rejection_same_side = (
            state.vwap_rejection_active
            and state.last_vwap_touch_side != 0
            and side == state.last_vwap_touch_side
            and abs_z < SIGNAL_ALERT_Z * 2  # override for extreme Z (≥6.0)
        )

        if (side != 0 and abs_z >= alert_z
            and not cooldown_active
            and not same_side_blocked
            and not cross_suppressed
            and not vwap_rejection_same_side
            and not anchor_blocked
            and not fade_opposes_drift):
            state.sig_state = 1
            state.alert_side = side
            state.peak_z = z_score
            state.action_signal = SIGNAL_STRETCH
            state.action_style = "cyan"
            # Record alert fire for per-side cooldown
            state.last_alert_fire_ts = now_ts
            state.last_alert_fire_side = side
            # MFE tracking: lock in entry, reset running favorable excursion
            state.alert_entry_ltp = state.ltp
            state.signal_mfe_points = 0.0
        else:
            state.action_signal = SIGNAL_NO_EDGE
            state.action_style = "dim white"
            if fade_opposes_drift:
                state.action_signal = SIGNAL_DRIFT_STAND_DOWN
                state.action_style = "magenta"
            elif cross_suppressed:
                state.action_signal += " | CROSS CAUTION"
            if vwap_rejection_same_side:
                state.action_signal += " | VWAP REJECTION ACTIVE"
            if anchor_blocked:
                state.action_signal += f" | {REGIME_ANCHOR_DIVERGE}"
            if same_side_blocked:
                state.action_signal += " | COOLDOWN"

    elif state.sig_state == 1:
        # Drift guard: if drift developed while in STRETCH, don't let it confirm
        # a fade against the drift (Z aligns with drift → fade fights it).
        alert_fade_opposes = drift_dir != 0 and state.alert_side * drift_dir > 0

        if side == state.alert_side and abs_z >= alert_z:
            if abs(z_score) > abs(state.peak_z):
                state.peak_z = z_score
            state.action_signal = SIGNAL_STRETCH
            state.action_style = "cyan"
        elif prev_abs_z > confirm_z and abs_z <= confirm_z:
            if alert_fade_opposes:
                # Would confirm a fade against drift — abort
                state.sig_state = 0
                state.alert_side = 0
                state.peak_z = 0.0
                state.action_signal = SIGNAL_DRIFT_STAND_DOWN
                state.action_style = "magenta"
                state.last_signal_exit_ts = now_ts
                state.conviction_score = 0
                state.setup_label = ""
            elif is_opening_discovery(now_ist):
                if abs_z < exit_z:
                    state.sig_state = 0
                    state.alert_side = 0
                    state.peak_z = 0.0
                    state.action_signal = SIGNAL_NO_EDGE
                    state.action_style = "dim white"
                    state.last_signal_exit_ts = now_ts
                    state.conviction_score = 0
                    state.setup_label = ""
                else:
                    state.action_signal = SIGNAL_STRETCH
                    state.action_style = "cyan"
            elif state.signal_mfe_points < _min_fav_pts:
                # Z reversed but price hasn't moved meaningfully — hold in ALERT.
                # The MFE-retry block below will finalize the CONFIRM on a later
                # tick if MFE catches up while Z stays in the exhausting band.
                state.action_signal = f"{SIGNAL_STRETCH} | MFE PENDING"
                state.action_style = "cyan"
            else:
                state.sig_state = 2
                state.action_signal, state.action_style = determine_confirmed_signal(
                    state.alert_side, state.peak_z, exhaustion_peak
                )
                # Institutional Phase 1: conviction + named setup on confirm
                state.conviction_score = compute_conviction_score(
                    state, instrument_key, state.alert_side, abs(state.peak_z),
                    alert_z, exhaustion_peak, er_threshold, hurst_threshold, drift_dir,
                )
                state.setup_label = classify_setup_label(
                    state, state.alert_side, state.peak_z, exhaustion_peak,
                    state.conviction_score, now_ist,
                )
                # Regime gate: drop known-bad (setup, regime) pairs into ABORT
                # instead of CONFIRM. Rules live in core.config.SETUP_REGIME_RULES.
                _allowed, _gate_reason = evaluate_regime_gate(
                    state.setup_label, state.regime, state.conviction_score,
                )
                if not _allowed:
                    _block_confirm_promotion(state, now_ts, _gate_reason)
        elif side != 0 and side != state.alert_side and abs_z >= alert_z:
            state.alert_side = side
            state.peak_z = z_score
            state.action_signal = SIGNAL_STRETCH
            state.action_style = "cyan"
            state.last_alert_fire_ts = now_ts
            state.last_alert_fire_side = side
            # Side flip — reset MFE tracking against the new entry
            state.alert_entry_ltp = state.ltp
            state.signal_mfe_points = 0.0
        elif abs_z < exit_z:
            state.sig_state = 0
            state.alert_side = 0
            state.peak_z = 0.0
            state.action_signal = SIGNAL_NO_EDGE
            state.action_style = "dim white"
            state.last_signal_exit_ts = now_ts
            state.conviction_score = 0
            state.setup_label = ""
        else:
            state.action_signal = SIGNAL_STRETCH
            state.action_style = "cyan"

    else:
        # Drift guard in execution: if drift now opposes the confirmed fade, exit
        alert_fade_opposes = drift_dir != 0 and state.alert_side * drift_dir < 0
        if alert_fade_opposes:
            state.sig_state = 0
            state.alert_side = 0
            state.peak_z = 0.0
            state.action_signal = SIGNAL_DRIFT_STAND_DOWN
            state.action_style = "magenta"
            state.last_signal_exit_ts = now_ts
            state.conviction_score = 0
            state.setup_label = ""
            reset_thesis_state(state)
        elif state.efficiency_ratio >= er_threshold and state.hurst >= REGIME_SHIFT_HURST_THRESHOLD:
            state.regime_shift_alert = True
            state.action_signal = SIGNAL_REGIME_SHIFT
            state.action_style = "bold red"
        else:
            state.regime_shift_alert = False
            state.action_signal, state.action_style = determine_confirmed_signal(
                state.alert_side, state.peak_z, exhaustion_peak
            )
        if state.sig_state == 2 and (side == 0 or side != state.alert_side or abs_z <= exit_z):
            state.sig_state = 0
            state.alert_side = 0
            state.peak_z = 0.0
            state.action_signal = SIGNAL_NO_EDGE
            state.action_style = "dim white"
            state.last_signal_exit_ts = now_ts
            state.conviction_score = 0
            state.setup_label = ""
            reset_thesis_state(state)

    # MFE-retry confirm: handles the case where Z reversed earlier with MFE
    # still under the bar. On a later tick, if MFE has now cleared the per-symbol
    # threshold AND Z is in the exhausting band (≤ confirm_z), upgrade to CONFIRM.
    # Drift opposition is rechecked here so a regime shift after the initial
    # Z-cross can still veto.
    if state.sig_state == 1 and abs_z <= confirm_z and state.signal_mfe_points >= _min_fav_pts:
        alert_fade_opposes_retry = drift_dir != 0 and state.alert_side * drift_dir > 0
        if not alert_fade_opposes_retry:
            state.sig_state = 2
            state.action_signal, state.action_style = determine_confirmed_signal(
                state.alert_side, state.peak_z, exhaustion_peak,
            )
            state.conviction_score = compute_conviction_score(
                state, instrument_key, state.alert_side, abs(state.peak_z),
                alert_z, exhaustion_peak, er_threshold, hurst_threshold, drift_dir,
            )
            state.setup_label = classify_setup_label(
                state, state.alert_side, state.peak_z, exhaustion_peak,
                state.conviction_score, now_ist,
            )
            # Regime gate (mirror of main-path check above).
            _allowed_r, _gate_reason_r = evaluate_regime_gate(
                state.setup_label, state.regime, state.conviction_score,
            )
            if not _allowed_r:
                _block_confirm_promotion(state, now_ts, _gate_reason_r)

    # MFE tracking cleanup: once we're back to sig_state=0 (any reset path),
    # clear the captured entry so the next 0→1 transition starts fresh.
    if state.sig_state == 0:
        state.alert_entry_ltp = 0.0
        state.signal_mfe_points = 0.0

    update_thesis_state(state, state.last_tick_ts or time.time())
    if (
        state.is_fakeout
        and state.action_signal in SIGNAL_CONFIRMED_SET
    ):
        state.action_signal = f"{state.action_signal} | {SIGNAL_FAKEOUT_PULLBACK}"
    if (
        state.action_signal in {SIGNAL_STRETCH} | SIGNAL_CONFIRMED_SET
        and is_outside_opening_range(state)
    ):
        state.action_signal = f"{state.action_signal} | {SIGNAL_BREAKOUT_ATTEMPT}"

    # V4.1: Alignment tags — add conviction when confirmed signal agrees with
    # both desk bias and the detected drift regime.
    _confirmed_long_signals = {SIGNAL_FADE_SCALP_LONG, SIGNAL_EXHAUSTION_SCALP_LONG, SIGNAL_MOMENTUM_LONG}
    _confirmed_short_signals = {SIGNAL_FADE_SCALP_SHORT, SIGNAL_EXHAUSTION_SCALP_SHORT, SIGNAL_MOMENTUM_SHORT}
    _base_signal = state.action_signal.split(" | ")[0]  # strip fakeout/breakout tags
    if _base_signal in _confirmed_long_signals or _base_signal in _confirmed_short_signals:
        _bias = compute_desk_bias()
        if ((_base_signal in _confirmed_long_signals and _bias["label"] == "Bullish")
                or (_base_signal in _confirmed_short_signals and _bias["label"] == "Bearish")):
            state.action_signal = f"{state.action_signal} | {SIGNAL_MACRO_ALIGNED}"
        if ((_base_signal in _confirmed_long_signals and drift_dir > 0)
                or (_base_signal in _confirmed_short_signals and drift_dir < 0)):
            state.action_signal = f"{state.action_signal} | {SIGNAL_DRIFT_ALIGNED}"

    # V3.0: Gamma flush override (highest priority)
    if state.gamma_flush_active and instrument_key == "NSE_INDEX|Nifty 50":
        if state.gamma_flush_side > 0:
            state.action_signal = SIGNAL_GAMMA_FLUSH_LONG
            state.action_style = "bold green on black"
        elif state.gamma_flush_side < 0:
            state.action_signal = SIGNAL_GAMMA_FLUSH_SHORT
            state.action_style = "bold red on black"

    # Institutional Phase 1: annotate confirmed signals with setup label + conviction stars
    if state.sig_state == 2 and state.setup_label and state.conviction_score > 0:
        stars = "★" * state.conviction_score + "·" * (5 - state.conviction_score)
        state.action_signal = f"[{state.setup_label} {stars}] {state.action_signal}"

    # Emit a row to logs/<date>_events.csv if sig_state transitioned this tick.
    # This is the ground-truth fire log that --review can grade against, instead
    # of re-deriving signals from raw z-score crossings.
    if prev_sig_state != state.sig_state:
        if prev_sig_state == 0 and state.sig_state == 1:
            evt, side = "ALERT", state.alert_side
        elif prev_sig_state == 1 and state.sig_state == 2:
            evt, side = "CONFIRM", state.alert_side
        elif prev_sig_state == 1 and state.sig_state == 0:
            evt, side = "ABORT", prev_alert_side
        elif prev_sig_state == 2 and state.sig_state == 0:
            evt, side = "EXIT", prev_alert_side
        else:
            evt, side = "STATE_CHANGE", prev_alert_side
        try:
            append_event_row(
                timestamp=state.last_tick_ts or time.time(),
                symbol=get_display_name(instrument_key),
                event_type=evt, side=side,
                z_score=state.z_score, ltp=state.ltp,
                regime=state.regime, setup_label=state.setup_label or "",
                conviction=state.conviction_score,
            )
        except Exception:
            pass

    state.prev_z = z_score


from orderflow.aggressor import (
    GAP_LARGE_PCT,
    GAP_SMALL_PCT,
    CVD_DIVERGENCE_DELTA_MIN,
    CVD_BREAKOUT_MIN_DELTA,
    CVD_ABSORPTION_5MIN_MIN,
    CVD_ABSORPTION_ER_MAX,
    CVD_DIVERGENCE_DECAY_SECS,
    _bucket_gap,
    classify_and_accumulate_aggressor,
    detect_flow_divergences,
)


# Spot symbols have vtt=0 so their CVD and divergence stay empty forever — the
# english_verdict for spot will always land in LOW-conviction branches. Mirror
# the corresponding futures verdict instead, since the underlying is the same.
SPOT_TO_FUT_DISPLAY_MIRROR: dict[str, str] = {
    "NIFTY":     "NIFTY_FUT",
    "BANKNIFTY": "BN_FUT",
}


def get_effective_verdict(instrument_key: str) -> tuple[str, str, str]:
    """Return (verdict, why, confidence) for display. For spot indices (which
    have no order-flow data), mirror the corresponding front-month futures
    verdict. Falls back to the symbol's own verdict if the FUT isn't resolved
    or its verdict is empty."""
    state = symbol_states.get(instrument_key)
    if state is None:
        return ("", "", "")
    own_display = DISPLAY_NAMES.get(instrument_key, "")
    target_display = SPOT_TO_FUT_DISPLAY_MIRROR.get(own_display)
    if target_display:
        for k, name in DISPLAY_NAMES.items():
            if name == target_display:
                fut_state = symbol_states.get(k)
                if fut_state is not None and fut_state.english_verdict:
                    return (fut_state.english_verdict, fut_state.english_why, fut_state.english_confidence)
                break
    return (state.english_verdict, state.english_why, state.english_confidence)


from signals.verdicts import compute_english_guidance


def build_log_row(instrument_key: str, state: SymbolState, timestamp: float) -> tuple[str, ...]:
    # 1-min buy/sell: prefer the IN-PROGRESS bar for tactical entries (per spec); the
    # completed-bar values are recoverable from the same series at the bar boundary.
    min_buy = state.minute_buy_vol if state.minute_buy_vol > 0 else state.last_completed_minute_buy
    min_sell = state.minute_sell_vol if state.minute_sell_vol > 0 else state.last_completed_minute_sell
    return (
        f"{timestamp:.2f}",
        get_display_name(instrument_key),
        f"{state.ltp:.2f}",
        f"{state.vwap:.2f}",
        f"{state.std_dev:.4f}",
        f"{state.z_score:+.4f}",
        state.action_signal,
        f"{state.efficiency_ratio:.4f}",
        f"{state.hurst:.4f}",
        state.regime,
        f"{state.cum_volume}",
        f"{state.oi:.0f}",
        f"{state.book_imbalance:+.3f}",
        f"{state.gap_pct:+.3f}",
        state.gap_bucket,
        state.english_verdict,
        f"{state.cvd:+d}",
        f"{min_buy}",
        f"{min_sell}",
        state.divergence_label,
    )


def update_pcr_snapshot(instrument_key: str, oi: float | None, timestamp: float) -> None:
    if oi is None or instrument_key not in PCR_KEY_SIDE:
        return

    strike = PCR_KEY_STRIKE[instrument_key]
    if PCR_KEY_SIDE[instrument_key] == "CE":
        pcr_state.ce_oi[strike] = oi
    else:
        pcr_state.pe_oi[strike] = oi
    pcr_state.last_updated = timestamp

    if pcr_state.oi_snapshot_ts is None or (timestamp - pcr_state.oi_snapshot_ts) >= 300:
        pcr_state.prev_ce_oi = pcr_state.ce_oi.copy()
        pcr_state.prev_pe_oi = pcr_state.pe_oi.copy()
        pcr_state.oi_snapshot_ts = timestamp
        nifty_st = symbol_states.get("NSE_INDEX|Nifty 50")
        pcr_state.ltp_at_oi_snapshot = nifty_st.ltp if nifty_st else 0.0


def update_option_greeks(
    instrument_key: str, iv: float, gamma: float,
    tbq: float, tsq: float, timestamp: float,
) -> None:
    """Store IV/gamma history for the strike and update TBQ/TSQ."""
    if instrument_key not in PCR_KEY_STRIKE:
        return
    strike = PCR_KEY_STRIKE[instrument_key]

    if strike not in pcr_state.iv_history:
        pcr_state.iv_history[strike] = deque(maxlen=GAMMA_FLUSH_HISTORY_MAXLEN)
    pcr_state.iv_history[strike].append((timestamp, iv))

    if strike not in pcr_state.gamma_history:
        pcr_state.gamma_history[strike] = deque(maxlen=GAMMA_FLUSH_HISTORY_MAXLEN)
    pcr_state.gamma_history[strike].append((timestamp, gamma))

    pcr_state.tbq_by_strike[strike] = tbq
    pcr_state.tsq_by_strike[strike] = tsq


def record_tick(
    instrument_key: str,
    ltp: float,
    vtt: int,
    timestamp: float,
    log_queue: asyncio.Queue | None,
    tick_ist: datetime | None = None,
    oi: float | None = None,
    book: tuple[float, float] | None = None,
    top_of_book: tuple[float | None, float | None] | None = None,
) -> None:
    state = symbol_states[instrument_key]
    if tick_ist is None:
        tick_ist = datetime.fromtimestamp(timestamp, IST)

    ensure_session_rollover(state, tick_ist)
    state.ltp = ltp
    state.last_tick_ts = timestamp
    if oi is not None and oi > 0:
        state.oi = oi
    if book is not None:
        tbq, tsq = book
        state.tbq = tbq
        state.tsq = tsq
        denom = tbq + tsq
        state.book_imbalance = (tbq - tsq) / denom if denom > 0 else 0.0
        # Phase 1 OFI — push the snapshot into the ring and compute the
        # smoothed delta (oldest→newest), then classify the 4-quadrant
        # absorption state by cross-referencing with CVD direction.
        # Observational only; no engine action triggers from this yet.
        state.tbq_history.append(tbq)
        state.tsq_history.append(tsq)
        if len(state.tbq_history) >= 2:
            d_tbq = state.tbq_history[-1] - state.tbq_history[0]
            d_tsq = state.tsq_history[-1] - state.tsq_history[0]
            state.delta_ofi_smoothed = d_tbq - d_tsq
            # OFI structural threshold (per-tick noise floor) and CVD-direction
            # threshold to call a "heavy" tape. Both tunable; start
            # conservative — we want absorption to be RARE and meaningful.
            _OFI_STRUCT = 25000.0
            _CVD_DIR = 5000
            if abs(state.delta_ofi_smoothed) >= _OFI_STRUCT and abs(state.cvd) >= _CVD_DIR:
                cvd_pos = state.cvd > 0
                ofi_pos = state.delta_ofi_smoothed > 0
                if not cvd_pos and ofi_pos:
                    # Tape selling, but limit-bid pile growing or asks pulling
                    # → passive demand absorbing aggressive supply.
                    state.absorption_label = "BULL_ABSORB"
                elif cvd_pos and not ofi_pos:
                    # Tape buying, but limit-ask pile growing or bids pulling
                    # → passive supply capping aggressive demand.
                    state.absorption_label = "BEAR_ABSORB"
                elif cvd_pos and ofi_pos:
                    # Tape buying AND limit-asks pulling → liquidity void up.
                    state.absorption_label = "BULL_VOID"
                else:
                    state.absorption_label = "BEAR_VOID"
            else:
                state.absorption_label = ""

    # Aggressor classification — runs early so CVD is ready by the time
    # divergence detection consumes it later in this tick.
    if vtt > 0:
        bid = ask = None
        if top_of_book is not None:
            bid, ask = top_of_book
        classify_and_accumulate_aggressor(state, ltp, vtt, bid, ask, tick_ist)
    # First tick of the day: seed prior_close from the pre-open prefetch, then capture gap
    if state.session_open is None and ltp > 0:
        if state.prior_close == 0.0:
            seeded = _prior_close_seed.get(instrument_key, 0.0)
            if seeded > 0:
                state.prior_close = seeded
        if state.prior_close > 0:
            state.gap_pct = (ltp / state.prior_close - 1.0) * 100.0
            state.gap_bucket = _bucket_gap(state.gap_pct)

    # V3.0: TPS counter (Nifty spot only)
    if instrument_key == "NSE_INDEX|Nifty 50":
        state._tps_timestamps.append(timestamp)
        tps_cutoff = timestamp - TPS_WINDOW_SECS
        while state._tps_timestamps and state._tps_timestamps[0] < tps_cutoff:
            state._tps_timestamps.popleft()
        state.tps = float(len(state._tps_timestamps))

    state.ticks.append(TickData(timestamp=timestamp, ltp=ltp, volume=vtt))

    idx = state._tick_count % DEQUE_MAXLEN
    state._timestamps[idx] = timestamp
    state._prices[idx] = ltp
    state._tick_count += 1
    update_structure_state(state, ltp, tick_ist)

    update_vwap(state, ltp, vtt)
    update_rolling_stddev(state, timestamp)
    update_zscore(state)
    # V5.2 Micro-Structural Layer — additive, does not alter sig_state
    update_micro_z(state, timestamp)
    update_z_velocity(state, timestamp)
    update_amber_state(state, timestamp)
    update_tick_rate(state, timestamp)
    state.vwap_slope = compute_vwap_slope(state, timestamp)
    state.price_slope_5m = compute_price_slope_5m(state, timestamp)
    er_prices = get_recent_prices_ordered(state, timestamp, ER_LOOKBACK_SECS)
    state.efficiency_ratio = compute_efficiency_ratio(er_prices)
    update_hurst_if_due(state, timestamp)

    # V4.0: Aggregate ticks into 1-min OHLC buckets and compute ATR
    update_ohlc_bucket(state, ltp, timestamp)
    compute_atr_from_buckets(state)

    update_signal_engine(instrument_key, state, now_ist=tick_ist)

    # V3.0: Gamma flush check (Nifty ticks only)
    if instrument_key == "NSE_INDEX|Nifty 50":
        flush_active, flush_side = check_gamma_flush(timestamp)
        state.gamma_flush_active = flush_active
        state.gamma_flush_side = flush_side

    # Flow divergences (consume CVD + or_high + ER computed above)
    detect_flow_divergences(state, tick_ist)

    # Plain-English decision layer — display-only, runs after all state updated
    verdict, why, conf = compute_english_guidance(state)
    state.english_verdict = verdict
    state.english_why = why
    state.english_confidence = conf

    if log_queue is not None:
        log_queue.put_nowait(build_log_row(instrument_key, state, timestamp))


def extract_feed_metrics(feed: pb.Feed) -> tuple[float, int, float | None] | None:
    oneof_field = feed.WhichOneof("FeedUnion")

    if oneof_field == "fullFeed":
        feed_type = feed.fullFeed.WhichOneof("FullFeedUnion")
        if feed_type == "marketFF":
            market_feed = feed.fullFeed.marketFF
            return market_feed.ltpc.ltp, market_feed.vtt, float(market_feed.oi)
        if feed_type == "indexFF":
            index_feed = feed.fullFeed.indexFF
            return index_feed.ltpc.ltp, 0, None
        return None

    if oneof_field == "ltpc":
        return feed.ltpc.ltp, 0, None

    if oneof_field == "firstLevelWithGreeks":
        level_feed = feed.firstLevelWithGreeks
        return level_feed.ltpc.ltp, level_feed.vtt, float(level_feed.oi)

    return None


def extract_book_pressure(feed: "pb.Feed") -> tuple[float, float] | None:
    """Pull total buy quantity / total sell quantity from a fullFeed.marketFF.
    Returns None for non-market feeds (indices have no order book)."""
    oneof_field = feed.WhichOneof("FeedUnion")
    if oneof_field != "fullFeed":
        return None
    feed_type = feed.fullFeed.WhichOneof("FullFeedUnion")
    if feed_type != "marketFF":
        return None
    mf = feed.fullFeed.marketFF
    tbq = float(getattr(mf, "tbq", 0.0) or 0.0)
    tsq = float(getattr(mf, "tsq", 0.0) or 0.0)
    if tbq <= 0 and tsq <= 0:
        return None
    return tbq, tsq


def extract_top_of_book(feed: "pb.Feed") -> tuple[float | None, float | None]:
    """Best bid + best ask from marketLevel.bidAskQuote[0], used for midpoint refinement
    of the aggressor classifier. Exchange APIs drop the depth array during volatility
    spikes (the exact moments we need it) — wrap defensively and return (None, None)
    so the math worker degrades gracefully instead of crashing the radar."""
    try:
        if feed.WhichOneof("FeedUnion") != "fullFeed":
            return None, None
        if feed.fullFeed.WhichOneof("FullFeedUnion") != "marketFF":
            return None, None
        quotes = feed.fullFeed.marketFF.marketLevel.bidAskQuote
        if not quotes:
            return None, None
        top = quotes[0]
        bp = float(top.bidP or 0.0)
        ap = float(top.askP or 0.0)
        if bp <= 0 or ap <= 0 or bp >= ap:
            return None, None
        return bp, ap
    except Exception:
        return None, None


def extract_option_greeks(feed: pb.Feed) -> tuple[float, float, float, float] | None:
    """Extract (iv, gamma, tbq, tsq) from a fullFeed.marketFF message.
    Returns None for non-marketFF feeds or if data is absent."""
    oneof_field = feed.WhichOneof("FeedUnion")
    if oneof_field != "fullFeed":
        return None
    feed_type = feed.fullFeed.WhichOneof("FullFeedUnion")
    if feed_type != "marketFF":
        return None
    mf = feed.fullFeed.marketFF
    iv = mf.iv
    gamma = mf.optionGreeks.gamma if mf.HasField("optionGreeks") else 0.0
    if iv == 0.0 and gamma == 0.0:
        return None
    return iv, gamma, mf.tbq, mf.tsq


async def process_message_receiver(raw, tick_queue, token_to_instrument):
    feed_response = pb.FeedResponse()
    feed_response.ParseFromString(raw)

    if feed_response.type == pb.market_info:
        return

    timestamp = time.time()
    for feed_key, feed in feed_response.feeds.items():
        metrics = extract_feed_metrics(feed)
        if metrics is None:
            continue

        ltp, vtt, oi = metrics
        inst_key = token_to_instrument.get(feed_key, feed_key)

        # 1. Update PCR options data (fast, no math)
        update_pcr_snapshot(inst_key, oi, timestamp)

        # 2. V3.0: Extract and store option greeks (ATM strikes only)
        if inst_key in PCR_KEY_SIDE:
            greeks = extract_option_greeks(feed)
            if greeks is not None:
                update_option_greeks(inst_key, *greeks, timestamp)

            # V4.0: Track per-strike option LTP + rolling OI history for RoC
            update_option_ltp(inst_key, ltp)
            if oi is not None:
                update_oi_roc_tracking(inst_key, oi, timestamp)

        # 3. Only push CORE assets to the heavy math queue
        if inst_key in symbol_states:
            book = extract_book_pressure(feed)
            top_of_book = extract_top_of_book(feed)
            tick_queue.put_nowait((inst_key, ltp, vtt, oi, book, top_of_book, timestamp))


async def compute_worker(tick_queue, log_queue):
    _sonar_tasks: set[asyncio.Task] = set()

    while True:
        # LIFO Safety: If queue > 500, skip stale ticks
        if tick_queue.qsize() > 500:
            while tick_queue.qsize() > 10:
                tick_queue.get_nowait()

        inst_key, ltp, vtt, oi, book, top_of_book, ts = await tick_queue.get()
        if inst_key in symbol_states:
            record_tick(inst_key, ltp, vtt, ts, log_queue, oi=oi, book=book, top_of_book=top_of_book)

            # V5.0: Fire Sonar query on significant signal (non-blocking)
            if _sonar_engine is not None:
                state = symbol_states[inst_key]
                display_name = get_display_name(inst_key)
                if (
                    abs(state.z_score) >= SONAR_SIGNAL_TRIGGER_Z
                    and display_name in SONAR_QUERY_INSTRUMENTS
                    and state.action_signal not in (SIGNAL_WARMING_UP, SIGNAL_NO_EDGE, "NO DATA")
                ):
                    task = asyncio.create_task(_sonar_check(display_name))
                    _sonar_tasks.add(task)
                    task.add_done_callback(_sonar_tasks.discard)

        tick_queue.task_done()


async def _sonar_check(display_name: str) -> None:
    """Background task: query Sonar and update global context cache."""
    if _sonar_engine is None:
        return
    try:
        ctx = await _sonar_engine.query_catalyst(display_name)
        _sonar_last_contexts[display_name] = ctx
    except Exception:
        pass  # Sonar failures are non-critical


def update_mock_pcr(timestamp: float) -> None:
    for index, strike in enumerate(PCR_STRIKES):
        ce_base = pcr_state.ce_oi.get(strike, 90_000 + (index * 3_000))
        pe_base = pcr_state.pe_oi.get(strike, 95_000 + (index * 3_000))
        pcr_state.ce_oi[strike] = max(1_000.0, ce_base + random.randint(-750, 750))
        pcr_state.pe_oi[strike] = max(1_000.0, pe_base + random.randint(-750, 750))

    pcr_state.last_updated = timestamp


def macro_direction_from_change(change_pct: float | None, invert: bool = False) -> int:
    if change_pct is None or abs(change_pct) < MACRO_BIAS_FLAT_CHANGE_PCT:
        return 0
    direction = 1 if change_pct > 0 else -1
    return -direction if invert else direction


def pcr_direction(ratio: float | None) -> int:
    if ratio is None:
        return 0
    if ratio >= PCR_BULLISH_THRESHOLD:
        return 1
    if ratio <= PCR_BEARISH_THRESHOLD:
        return -1
    return 0


def get_live_nifty_ltp() -> float | None:
    nifty_key = "NSE_INDEX|Nifty 50"
    state = symbol_states.get(nifty_key)
    if state is None or state.ltp <= 0:
        return None
    return state.ltp


def usdinr_direction() -> tuple[int, str]:
    usdinr_key = next((key for key in USDINR_KEYS if key in symbol_states), None)
    if usdinr_key is None:
        return 0, "USDINR: awaiting live"

    state = symbol_states[usdinr_key]
    if state.session_open is None or state.ltp == 0.0:
        return 0, "USDINR: awaiting open"

    move = state.ltp - state.session_open
    if abs(move) < USDINR_FLAT_MOVE:
        direction = 0
    else:
        direction = -1 if move > 0 else 1
    return direction, f"USDINR: {state.ltp:,.2f} vs open {state.session_open:,.2f}"


def get_vix_state() -> tuple[float, str, str]:
    """Return (vix_value, vix_regime_label, vix_text) from live India VIX data."""
    vix_state = symbol_states.get(VIX_INSTRUMENT_KEY)
    if vix_state is None or vix_state.ltp <= 0:
        return 0.0, "---", "VIX: awaiting"
    vix = vix_state.ltp
    # Intraday change for crush/spike detection
    change_pct = 0.0
    if vix_state.session_open and vix_state.session_open > 0:
        change_pct = ((vix - vix_state.session_open) / vix_state.session_open) * 100
    if vix >= VIX_HIGH_THRESHOLD:
        regime = "FEAR"
    elif vix <= VIX_LOW_THRESHOLD:
        regime = "COMPLACENT"
    else:
        regime = "NORMAL"
    # Overlay crush/spike
    tag = ""
    if change_pct <= VIX_CRUSH_THRESHOLD:
        tag = " CRUSH"
    elif change_pct >= VIX_SPIKE_THRESHOLD:
        tag = " SPIKE"
    return vix, f"{regime}{tag}", f"VIX: {vix:.2f} ({change_pct:+.1f}%) {regime}{tag}"


def vix_z_scale() -> float:
    """Return a multiplier for Z-score thresholds based on India VIX level.
    High VIX → widen thresholds (need bigger move to signal).
    Low VIX → tighten thresholds (smaller moves are meaningful)."""
    vix_state = symbol_states.get(VIX_INSTRUMENT_KEY)
    if vix_state is None or vix_state.ltp <= 0:
        return 1.0
    vix = vix_state.ltp
    if vix >= VIX_HIGH_THRESHOLD:
        return VIX_HIGH_SCALE
    elif vix <= VIX_LOW_THRESHOLD:
        return VIX_LOW_SCALE
    return 1.0


def compute_desk_bias() -> dict[str, object]:
    usdinr_dir, usdinr_text = usdinr_direction()
    nifty_ltp = get_live_nifty_ltp()
    pcr_ratio = pcr_state.get_dynamic_ratio(nifty_ltp) if nifty_ltp is not None else None
    pcr_dir_value = pcr_direction(pcr_ratio)

    total_score = (
        usdinr_dir * MACRO_BIAS_WEIGHTS["USDINR"]
        + pcr_dir_value * MACRO_BIAS_WEIGHTS["PCR"]
    )

    if total_score >= MACRO_BIAS_BULLISH_THRESHOLD:
        label = "Bullish"
    elif total_score <= MACRO_BIAS_BEARISH_THRESHOLD:
        label = "Bearish"
    else:
        label = "Mixed"

    # VIX overlay: if VIX is spiking, flag caution regardless of bias direction
    vix_val, vix_regime, vix_text = get_vix_state()
    if "SPIKE" in vix_regime:
        label = f"{label} | VIX SPIKE"

    pcr_text = "PCR(ATM±100): awaiting OI" if pcr_ratio is None else f"PCR(ATM±100): {pcr_ratio:.2f}"
    return {
        "label": label,
        "score": total_score,
        "usdinr_text": usdinr_text,
        "pcr_text": pcr_text,
        "vix_text": vix_text,
        "vix_regime": vix_regime,
    }


def format_macro_quote(label: str) -> str:
    quote_state = macro_state.quotes[label]
    if quote_state.value is None:
        suffix = "awaiting feed"
        if quote_state.error:
            suffix = "unavailable"
        return f"{label}: {suffix}"

    change_text = "n/a" if quote_state.change_pct is None else f"{quote_state.change_pct:+.2f}%"
    return f"{label}: {quote_state.value:,.2f} ({change_text})"


# Rich TUI helpers (format_macro_header, focus_sort_key, build_focus_panel,
# format_thesis_readout, format_pcr_footer, format_v4_footer) moved to
# ui/terminal.py. Imported below alongside the panel renders + build_dashboard
# so the legacy Rich Live main loop in this module still resolves them.


# --------------------------- INDEX DRIVER PANEL (Phase 2) ---------------------------

def _get_recent_ts_prices(
    state: SymbolState, now: float, lookback_secs: int
) -> tuple[np.ndarray, np.ndarray]:
    n = min(state._tick_count, DEQUE_MAXLEN)
    if n == 0:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    if n < DEQUE_MAXLEN:
        ts = state._timestamps[:n]
        pr = state._prices[:n]
    else:
        start = state._tick_count % DEQUE_MAXLEN
        ts = np.concatenate((state._timestamps[start:], state._timestamps[:start]))
        pr = np.concatenate((state._prices[start:], state._prices[:start]))
    cutoff = now - lookback_secs
    mask = ts >= cutoff
    return ts[mask], pr[mask]


def _resample_1s(ts: np.ndarray, pr: np.ndarray, start_ts: float, end_ts: float) -> np.ndarray:
    """Last-price-per-1sec-bin grid. Forward-fills gaps."""
    if ts.size == 0 or end_ts <= start_ts:
        return np.array([], dtype=np.float64)
    n_bins = int(end_ts - start_ts) + 1
    grid = np.full(n_bins, np.nan, dtype=np.float64)
    bins = np.floor(ts - start_ts).astype(int)
    mask = (bins >= 0) & (bins < n_bins)
    for b, p in zip(bins[mask], pr[mask]):
        grid[b] = p  # last-write-wins since ts is in order
    last = np.nan
    for i in range(n_bins):
        if np.isnan(grid[i]):
            grid[i] = last
        else:
            last = grid[i]
    return grid


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or b.size < 2 or a.size != b.size:
        return 0.0
    a_std = float(a.std())
    b_std = float(b.std())
    if a_std == 0.0 or b_std == 0.0:
        return 0.0
    return float(((a - a.mean()) * (b - b.mean())).mean() / (a_std * b_std))


def _grid_log_returns(grid: np.ndarray) -> np.ndarray:
    if grid.size < 2 or np.isnan(grid).any() or (grid <= 0).any():
        return np.array([], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(grid))
    return r[np.isfinite(r)]


def _compute_pair_metrics(key_a: str, key_b: str, now: float) -> DriverMetric:
    m = DriverMetric(pair=(key_a, key_b))
    state_a = symbol_states.get(key_a)
    state_b = symbol_states.get(key_b)
    if state_a is None or state_b is None or state_a.ltp <= 0 or state_b.ltp <= 0:
        return m

    ts_a, pr_a = _get_recent_ts_prices(state_a, now, INDEX_DRIVER_LOOKBACK_SECS)
    ts_b, pr_b = _get_recent_ts_prices(state_b, now, INDEX_DRIVER_LOOKBACK_SECS)
    if ts_a.size < 10 or ts_b.size < 10:
        return m

    start_ts = float(max(ts_a[0], ts_b[0]))
    end_ts = float(min(ts_a[-1], ts_b[-1]))
    if end_ts - start_ts < INDEX_DRIVER_MIN_POINTS:
        return m

    grid_a = _resample_1s(ts_a, pr_a, start_ts, end_ts)
    grid_b = _resample_1s(ts_b, pr_b, start_ts, end_ts)
    r_a = _grid_log_returns(grid_a)
    r_b = _grid_log_returns(grid_b)
    n = min(r_a.size, r_b.size)
    if n < INDEX_DRIVER_MIN_POINTS:
        return m
    r_a = r_a[-n:]
    r_b = r_b[-n:]

    m.corr = _pearson(r_a, r_b)
    m.n_points = n

    best_corr = m.corr
    best_lag = 0
    min_overlap = INDEX_DRIVER_MIN_POINTS // 2
    for lag in range(
        -INDEX_DRIVER_LAG_MAX_SECS,
        INDEX_DRIVER_LAG_MAX_SECS + 1,
        INDEX_DRIVER_LAG_STEP_SECS,
    ):
        if lag == 0:
            continue
        if lag > 0:
            x, y = r_a[:-lag], r_b[lag:]
        else:
            x, y = r_a[-lag:], r_b[:lag]
        if x.size < min_overlap:
            continue
        c = _pearson(x, y)
        if abs(c) > abs(best_corr):
            best_corr = c
            best_lag = lag
    m.lead_lag_secs = best_lag
    m.lead_corr = best_corr

    za = state_a.z_score
    zb = state_b.z_score
    name_a = get_display_name(key_a)
    name_b = get_display_name(key_b)
    if za * zb < 0 and abs(za) >= 0.5 and abs(zb) >= 0.5:
        m.drag = "DIVERGE"
        m.drag_detail = f"{name_a} Z{za:+.2f} vs {name_b} Z{zb:+.2f} (opposite sides)"
    elif abs(za) >= 0.7 and abs(zb) < 0.3:
        m.drag = "DRAG"
        m.drag_detail = f"{name_a} moving (Z{za:+.2f}) without {name_b} (Z{zb:+.2f})"
    elif abs(zb) >= 0.7 and abs(za) < 0.3:
        m.drag = "DRAG"
        m.drag_detail = f"{name_b} moving (Z{zb:+.2f}) without {name_a} (Z{za:+.2f})"
    elif za * zb > 0 and abs(zb) > abs(za) and abs(zb) >= 0.5:
        m.drag = "BOOST"
        m.drag_detail = f"{name_b} amplifying {name_a} (Z{zb:+.2f} vs {za:+.2f})"
    m.stale = False
    return m


def refresh_index_driver_metrics(now: float) -> None:
    if now - index_driver_state.last_refresh_ts < INDEX_DRIVER_REFRESH_SECS:
        return
    index_driver_state.metrics = [
        _compute_pair_metrics(a, b, now) for (a, b) in INDEX_DRIVER_PAIRS
    ]
    index_driver_state.last_refresh_ts = now
    refresh_driver_acceleration(now)


def refresh_driver_acceleration(now: float) -> None:
    """V5.2 Layer 4: fire NIFTY amber when HDFCBANK/RELIANCE z_velocity spikes but NIFTY Z still flat.

    Heavyweight components reprice 5-30s before the index options chain
    catches up on flow-driven moves. This layer surfaces that lead as an
    amber chip on NIFTY — advisory only, no trade trigger.

    Safeguards:
      - Suppressed if |NIFTY Z| ≥ DRIVER_ACCEL_NIFTY_MAX_Z (full alert or
        already extended — no front-running needed).
      - Suppressed if NIFTY sig_state ≥ 1 (full alert subsumes amber).
      - Driver Z and Z-velocity must share sign (same-direction acceleration,
        not a reversion bounce).
    """
    nifty = symbol_states.get("NSE_INDEX|Nifty 50")
    if nifty is None:
        return

    if nifty.sig_state >= 1 or abs(nifty.z_score) >= DRIVER_ACCEL_NIFTY_MAX_Z:
        if nifty.amber_reason == "DRIVER":
            nifty.amber_active = False
            nifty.amber_side = 0
            nifty.amber_reason = ""
        index_driver_state.nifty_driver_amber = False
        index_driver_state.nifty_driver_amber_side = 0
        index_driver_state.nifty_driver_amber_source = ""
        index_driver_state.nifty_driver_amber_velocity = 0.0
        return

    best_abs_vel = 0.0
    best_side = 0
    best_source = ""
    best_vel = 0.0
    for key in DRIVER_ACCEL_SOURCES:
        s = symbol_states.get(key)
        if s is None:
            continue
        if abs(s.z_velocity) < DRIVER_ACCEL_THRESHOLD:
            continue
        # Same-sign Z × velocity = continuation, not reversion
        if s.z_score * s.z_velocity <= 0:
            continue
        if abs(s.z_velocity) > best_abs_vel:
            best_abs_vel = abs(s.z_velocity)
            best_side = 1 if s.z_velocity > 0 else -1
            best_source = get_display_name(key)
            best_vel = s.z_velocity

    if best_side != 0:
        index_driver_state.nifty_driver_amber = True
        index_driver_state.nifty_driver_amber_side = best_side
        index_driver_state.nifty_driver_amber_source = best_source
        index_driver_state.nifty_driver_amber_velocity = best_vel
        nifty.amber_active = True
        nifty.amber_side = best_side
        nifty.amber_reason = "DRIVER"
    else:
        index_driver_state.nifty_driver_amber = False
        index_driver_state.nifty_driver_amber_side = 0
        index_driver_state.nifty_driver_amber_source = ""
        index_driver_state.nifty_driver_amber_velocity = 0.0
        if nifty.amber_reason == "DRIVER":
            nifty.amber_active = False
            nifty.amber_side = 0
            nifty.amber_reason = ""


# build_index_driver_panel moved to ui/terminal.py.


# --- OI LEVELS PANEL (Phase 3) ---
# refresh_oi_levels + _compute_max_pain + _find_gamma_walls moved to
# orderflow/oi_levels.py. Re-exported here for existing call sites.
from orderflow.oi_levels import refresh_oi_levels, _compute_max_pain, _find_gamma_walls  # noqa: F401


# build_oi_levels_panel moved to ui/terminal.py.


# build_pre_open_panel + _verdict_style moved to ui/terminal.py.


# All TUI panel renders + build_dashboard moved to ui/terminal.py.
# Imported below so the legacy Rich Live main loops (run_replay, run_review,
# run_live_radar) keep resolving build_dashboard.
from ui.terminal import (  # noqa: F401
    format_macro_header,
    focus_sort_key,
    build_focus_panel,
    format_thesis_readout,
    format_pcr_footer,
    format_v4_footer,
    build_index_driver_panel,
    build_oi_levels_panel,
    build_pre_open_panel,
    build_dashboard,
)


def signal_outcome(
    side: int, entry_price: float, mfe_points: float | None, min_favorable_pts: float,
) -> tuple[str, float | None]:
    """Grade a signal by maximum favorable excursion vs a meaningful-move threshold.

    A 2-pt favorable tick at +15m is noise — only count as a win when the move
    actually crossed `min_favorable_pts` at some point in the window.
    """
    if side == 0 or mfe_points is None:
        return "NO RULE", None
    if entry_price > 0:
        favorable_pct = (mfe_points / entry_price) * 100
    else:
        favorable_pct = None
    if mfe_points >= min_favorable_pts:
        return "OK MOVED", favorable_pct
    return "X WEAK", favorable_pct


def calculate_mae_points(side: int, entry_price: float, window_prices: np.ndarray) -> float | None:
    if window_prices.size == 0: return None
    if side > 0: adverse_move = float(np.max(window_prices)) - entry_price
    elif side < 0: adverse_move = entry_price - float(np.min(window_prices))
    else: return None
    return max(0.0, adverse_move)


def calculate_mfe_points(side: int, entry_price: float, window_prices: np.ndarray) -> float | None:
    """Max favorable excursion: best point in the forward window measured in the
    signal's expected direction. side=+1 (fade short) → favorable = price falls
    below entry. side=-1 (fade long) → favorable = price rises above entry."""
    if window_prices.size == 0: return None
    if side > 0: favorable_move = entry_price - float(np.min(window_prices))
    elif side < 0: favorable_move = float(np.max(window_prices)) - entry_price
    else: return None
    return max(0.0, favorable_move)


def resolve_min_favorable_pts(
    symbol: str, entry_price: float, override: float | None,
    atr_proxy: float = 0.0,
) -> float:
    """Per-symbol MFE threshold = max(MFE_ATR_K × atr_proxy, per-symbol floor).

    Priority for the floor: explicit CLI override > MIN_FAVORABLE_POINTS_PER_SYMBOL
    > percentage-of-entry fallback. ATR scaling lifts the bar on volatile days
    while the floor protects against rewarding micro-grabs in calm regimes.
    """
    if override is not None and override > 0:
        floor = float(override)
    else:
        table_value = MIN_FAVORABLE_POINTS_PER_SYMBOL.get(symbol)
        if table_value is not None:
            floor = float(table_value)
        else:
            floor = abs(entry_price) * (REVIEW_DEFAULT_MIN_FAVORABLE_PCT / 100.0)
    atr_scaled = MFE_ATR_K * max(0.0, float(atr_proxy))
    return max(atr_scaled, floor)


def compute_atr_proxy(
    timestamps: np.ndarray, prices: np.ndarray, signal_index: int,
    window_secs: int = MFE_ATR_PROXY_WINDOW_SECS,
) -> float:
    """Approximate 5-min ATR from the per-tick price series in the review path.

    Defined as `max(prices) - min(prices)` over the `window_secs` immediately
    BEFORE the signal — i.e., the realized range the engine just saw. Returns
    0.0 if there aren't enough prior ticks (cold start near session open).
    """
    if signal_index <= 0 or timestamps.size == 0 or prices.size == 0:
        return 0.0
    signal_ts = float(timestamps[signal_index])
    window_start_ts = signal_ts - float(window_secs)
    start_index = int(np.searchsorted(timestamps, window_start_ts, side="left"))
    start_index = max(0, min(start_index, signal_index))
    if start_index >= signal_index:
        return 0.0
    window = prices[start_index:signal_index + 1]
    if window.size == 0:
        return 0.0
    return float(np.max(window) - np.min(window))


def calculate_mae_z(side: int, entry_z: float, window_z_scores: np.ndarray) -> float | None:
    if window_z_scores.size == 0: return None
    if side > 0: adverse_move = float(np.max(window_z_scores)) - entry_z
    elif side < 0: adverse_move = entry_z - float(np.min(window_z_scores))
    else: return None
    return max(0.0, adverse_move)


def classify_signal_from_z(z_score: float, exhaustion_peak: float = SIGNAL_EXHAUSTION_PEAK) -> tuple[str, int]:
    side = 1 if z_score > 0 else -1 if z_score < 0 else 0
    signal, _ = determine_confirmed_signal(side, z_score, exhaustion_peak)
    return signal, side


def build_touch_review_events(symbol_frame: pd.DataFrame, min_z: float) -> list[dict[str, float | int | str]]:
    events = []
    active_side = 0
    for event_index, row in enumerate(symbol_frame.itertuples(index=False)):
        z_score = float(row.z_score)
        abs_z = abs(z_score)
        side = 1 if z_score > 0 else -1 if z_score < 0 else 0
        if side != 0 and abs_z >= min_z:
            if active_side != side:
                signal, event_side = classify_signal_from_z(z_score)
                events.append({"entry_index": int(event_index), "timestamp": float(row.timestamp), "signal": signal, "side": event_side, "entry_ltp": float(row.ltp), "entry_z": z_score})
                active_side = side
        elif abs_z < min_z:
            active_side = 0
    return events


def build_confirm_review_events(symbol_frame: pd.DataFrame, min_z: float) -> list[dict[str, float | int | str]]:
    events = []
    active_side = 0
    active_peak_z = 0.0
    for event_index, row in enumerate(symbol_frame.itertuples(index=False)):
        z_score = float(row.z_score)
        abs_z = abs(z_score)
        side = 1 if z_score > 0 else -1 if z_score < 0 else 0
        if side != 0 and abs_z >= min_z:
            if active_side != side:
                active_side = side
                active_peak_z = z_score
            elif abs_z >= abs(active_peak_z):
                active_peak_z = z_score
            continue
        if active_side != 0 and abs_z < min_z:
            signal, _ = determine_confirmed_signal(active_side, active_peak_z, SIGNAL_EXHAUSTION_PEAK)
            events.append({"entry_index": int(event_index), "timestamp": float(row.timestamp), "signal": signal, "side": active_side, "entry_ltp": float(row.ltp), "entry_z": z_score})
            active_side = 0
            active_peak_z = 0.0
    return events


# --------------------------- REVIEW MODE ---------------------------

def build_review_rows(
    frame: pd.DataFrame, min_z: float, window_minutes: int, entry_mode: str = REVIEW_ENTRY_TOUCH,
    min_favorable_pts_override: float | None = None,
) -> list[dict[str, object]]:
    review_rows = []
    window_secs = window_minutes * 60

    for symbol, symbol_frame in frame.groupby("symbol", sort=False):
        symbol_frame = symbol_frame.sort_values("timestamp").reset_index(drop=True)
        timestamps = symbol_frame["timestamp"].to_numpy(dtype=float)
        prices = symbol_frame["ltp"].to_numpy(dtype=float)
        z_scores = symbol_frame["z_score"].to_numpy(dtype=float)

        if entry_mode == REVIEW_ENTRY_CONFIRM:
            review_events = build_confirm_review_events(symbol_frame, min_z)
        else:
            review_events = build_touch_review_events(symbol_frame, min_z)

        for review_event in review_events:
            event_timestamp = float(review_event["timestamp"])
            if not is_after_review_start(event_timestamp):
                continue

            event_index = int(review_event["entry_index"])
            target_timestamp = event_timestamp + window_secs
            window_end_index = int(np.searchsorted(timestamps, target_timestamp, side="right") - 1)
            available_end_index = min(window_end_index, len(symbol_frame) - 1)
            side = int(review_event["side"])
            entry_ltp = float(review_event["entry_ltp"])
            window_slice = slice(event_index, available_end_index + 1)
            window_prices = prices[window_slice]
            window_z_scores = z_scores[window_slice]
            mae_points = calculate_mae_points(side, entry_ltp, window_prices)
            mae_z = calculate_mae_z(side, float(review_event["entry_z"]), window_z_scores)
            mfe_points = calculate_mfe_points(side, entry_ltp, window_prices)
            # ATR proxy over the prior MFE_ATR_PROXY_WINDOW_SECS — gives the
            # threshold something to scale against on volatile days.
            atr_proxy = compute_atr_proxy(timestamps, prices, event_index)
            min_favorable_pts = resolve_min_favorable_pts(
                str(symbol), entry_ltp, min_favorable_pts_override, atr_proxy,
            )

            if available_end_index < event_index or timestamps[-1] < target_timestamp:
                forward_price = None
                result = f"NO +{window_minutes}M DATA"
                favorable_move_pct = None
            else:
                forward_price = float(prices[available_end_index])
                result, favorable_move_pct = signal_outcome(
                    side, entry_ltp, mfe_points, min_favorable_pts,
                )

            review_rows.append({
                "timestamp": event_timestamp, "symbol": str(symbol), "signal": SIGNAL_ABBREVIATIONS.get(str(review_event["signal"]), str(review_event["signal"])),
                "z_score": float(review_event["entry_z"]), "entry_ltp": entry_ltp, "forward_ltp": forward_price,
                "mae_points": mae_points, "mae_z": mae_z, "mfe_points": mfe_points,
                "min_favorable_pts": min_favorable_pts,
                "result": result, "favorable_move_pct": favorable_move_pct,
            })

    review_rows.sort(key=lambda item: (item["timestamp"], item["symbol"]))
    return review_rows


def render_review(
    review_day: date, review_rows: list[dict[str, object]], min_z: float, window_minutes: int, entry_mode: str,
    min_favorable_pts_override: float | None = None,
) -> None:
    table = Table(title=f"POST-MARKET REVIEW - {review_day.isoformat()}", header_style="bold cyan", border_style="blue")
    table.add_column("Time", width=8); table.add_column("Symbol", width=12); table.add_column("Signal", width=10)
    table.add_column("Z-Score", justify="right", width=9); table.add_column("LTP @sig", justify="right", width=12)
    table.add_column(f"LTP +{window_minutes}m", justify="right", width=12)
    table.add_column("MFE pts", justify="right", width=10)
    table.add_column("MAE pts", justify="right", width=10)
    table.add_column("Min Fav", justify="right", width=8)
    table.add_column("Result", width=14)

    evaluated_rows = [row for row in review_rows if row["forward_ltp"] is not None]
    moved_rows = [row for row in evaluated_rows if row["result"] == "OK MOVED"]
    weak_rows = [row for row in evaluated_rows if row["result"] == "X WEAK"]

    accuracy = (len(moved_rows) / len(evaluated_rows) * 100) if evaluated_rows else 0.0
    mean_favorable = (sum(float(row["favorable_move_pct"]) for row in moved_rows if row.get("favorable_move_pct") is not None) / len(moved_rows) if moved_rows else 0.0)
    incomplete = len([row for row in review_rows if row["forward_ltp"] is None])

    if not review_rows:
        table.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-", "No signal events")
    else:
        for row in review_rows:
            timestamp = datetime.fromtimestamp(float(row["timestamp"]), IST).strftime("%H:%M")
            forward_ltp = "-" if row["forward_ltp"] is None else f"{float(row['forward_ltp']):,.2f}"
            mae_points = "-" if row["mae_points"] is None else f"{float(row['mae_points']):,.2f}"
            mfe_points = "-" if row.get("mfe_points") is None else f"{float(row['mfe_points']):,.2f}"
            min_fav = "-" if row.get("min_favorable_pts") is None else f"{float(row['min_favorable_pts']):,.2f}"
            result_style = "green" if row["result"] == "OK MOVED" else "red" if row["result"] == "X WEAK" else "yellow"

            table.add_row(timestamp, str(row["symbol"]), str(row["signal"]), f"{float(row['z_score']):+.2f}", f"{float(row['entry_ltp']):,.2f}", forward_ltp, mfe_points, mae_points, min_fav, Text(str(row["result"]), style=result_style))

    symbol_summary = Table(title="Per-Symbol Summary", header_style="bold cyan", border_style="green")
    symbol_summary.add_column("Symbol", width=12); symbol_summary.add_column("Signals", justify="right", width=8)
    symbol_summary.add_column("Accuracy", justify="right", width=10)
    symbol_summary.add_column("Avg MFE", justify="right", width=10)
    symbol_summary.add_column("Avg MAE", justify="right", width=10)

    if evaluated_rows:
        summary_frame = pd.DataFrame({
            "symbol": [str(row["symbol"]) for row in evaluated_rows],
            "result": [str(row["result"]) for row in evaluated_rows],
            "mae_points": [np.nan if row["mae_points"] is None else float(row["mae_points"]) for row in evaluated_rows],
            "mfe_points": [np.nan if row.get("mfe_points") is None else float(row["mfe_points"]) for row in evaluated_rows],
        })
        grouped = summary_frame.groupby("symbol", sort=False).agg(
            signals=("symbol", "size"),
            moved=("result", lambda series: int((series == "OK MOVED").sum())),
            avg_mfe=("mfe_points", "mean"),
            avg_mae=("mae_points", "mean"),
        ).reset_index()
        for row in grouped.itertuples(index=False):
            accuracy_text = f"{(row.moved / row.signals * 100):.1f}%"
            symbol_summary.add_row(str(row.symbol), f"{int(row.signals)}", accuracy_text, f"{float(row.avg_mfe):,.2f}", f"{float(row.avg_mae):,.2f}")
    else:
        symbol_summary.add_row("-", "0", "0.0%", "-", "-")

    if min_favorable_pts_override is not None:
        threshold_label = f"override {min_favorable_pts_override:.2f} pts"
    else:
        threshold_label = "per-symbol (see Min Fav col)"
    summary = (
        f"Review start: 09:45 IST  |  |Z| >= {min_z:.1f}  |  Window: {window_minutes}m  |  Entry: {entry_mode}  |  "
        f"Min favorable: {threshold_label}  |  "
        f"ACCURACY: {accuracy:.1f}%  |  Avg favorable: +{mean_favorable:.2f}%  |  Weak/noise: {len(weak_rows)}  |  Incomplete: {incomplete}"
    )
    console.print(table)
    console.print(symbol_summary)
    console.print(Panel(summary, title="Review Summary", border_style="green"))


def run_review(
    review_day: date, min_z: float, window_minutes: int, entry_mode: str,
    min_favorable_pts: float | None = None,
) -> int:
    if window_minutes <= 0: console.print("[bold red]Review window must be at least 1 minute.[/bold red]"); return 1
    if entry_mode not in REVIEW_ENTRY_MODES: console.print(f"[bold red]Unknown review entry mode: {entry_mode}[/bold red]"); return 1

    log_dir = get_log_dir(review_day)
    legacy_path = get_log_path(review_day)
    frames: list[pd.DataFrame] = []
    if log_dir.exists() and log_dir.is_dir():
        for csv_path in sorted(log_dir.glob("*.csv")):
            try:
                frames.append(pd.read_csv(csv_path))
            except Exception as exc:
                console.print(f"[yellow]Skipped {csv_path.name}: {exc}[/yellow]")
    elif legacy_path.exists():
        frames.append(pd.read_csv(legacy_path))
    else:
        console.print(f"[bold red]No log data for {review_day}: looked in {log_dir}/ and {legacy_path.as_posix()}[/bold red]")
        return 1

    if not frames:
        console.print(f"[bold red]Log directory {log_dir}/ contained no readable CSVs.[/bold red]")
        return 1

    frame = pd.concat(frames, ignore_index=True)
    missing_columns = [column for column in REQUIRED_LOG_COLUMNS if column not in frame.columns]
    if missing_columns: console.print("[bold red]Review failed: missing columns " + ", ".join(missing_columns) + "[/bold red]"); return 1

    if frame.empty: console.print("[yellow]Log file is empty.[/yellow]"); return 0

    frame = frame.copy()
    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce")
    frame["ltp"] = pd.to_numeric(frame["ltp"], errors="coerce")
    frame["z_score"] = pd.to_numeric(frame["z_score"], errors="coerce")
    frame["signal"] = frame["signal"].astype(str).str.strip()
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame = frame.dropna(subset=["timestamp", "ltp", "z_score"])
    frame = frame.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    review_rows = build_review_rows(
        frame, min_z, window_minutes, entry_mode,
        min_favorable_pts_override=min_favorable_pts,
    )
    if not review_rows: console.print("[yellow]No signals found after the 09:45 AM filter window.[/yellow]"); return 0

    render_review(
        review_day, review_rows, min_z, window_minutes, entry_mode,
        min_favorable_pts_override=min_favorable_pts,
    )
    return 0


# --------------------------- ASYNC TASKS ---------------------------


# disk_writer_task lives in sentinel.io_logs (imported earlier via the io_logs façade block)


async def resolve_upstox_token(session: aiohttp.ClientSession, symbol: str, exchange_hint: str) -> str:
    if not exchange_hint or "|" not in f"{exchange_hint}|{symbol}": raise ValueError("Exchange hint is required to resolve Upstox token for symbol")
    return await resolve_historical_instrument_key(session, f"{exchange_hint}|{symbol}")


async def ws_task(log_queue: asyncio.Queue, tick_queue: asyncio.Queue) -> None:
    token_to_instrument: dict[str, str] = {}
    
    if not resolve_mock_mode():
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            resolved_keys: dict[str, str] = {}
            for key in SUBSCRIPTION_KEYS:
                # BYPASS Spot Indices (name-based keys work for indices on both
                # NSE and BSE — Nifty 50, India VIX, SENSEX, etc.).
                if "NSE_INDEX" in key or "BSE_INDEX" in key:
                    resolved_keys[key] = key
                    continue

                if instrument_key_uses_numeric_token(key):
                    resolved_keys[key] = key
                    continue

                try:
                    resolved_key = await resolve_historical_instrument_key(session, key)
                    resolved_keys[key] = resolved_key
                except Exception as exc:
                    if key in PCR_KEY_SIDE:
                        console.print(f"[yellow]Warning: skipping unresolved PCR option key {key}: {exc}[/yellow]")
                        continue
                    console.print(f"[yellow]Warning: could not resolve key {key}: {exc}[/yellow]")
                    resolved_keys[key] = key

            token_to_instrument = {
                resolved_key: original_key
                for original_key, resolved_key in resolved_keys.items()
            }
            subscription_keys = list(dict.fromkeys(resolved_keys.values()))
    else:
        subscription_keys = SUBSCRIPTION_KEYS
        
    radar_task = None
    while True:
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            async with websockets.connect(
                WS_URL,
                ssl=ssl_ctx,
                additional_headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            ) as websocket:
                await asyncio.sleep(1)

                sub_request = {
                    "guid": "gammaleak",
                    "method": "sub",
                    "data": {
                        "mode": "full",
                        "instrumentKeys": subscription_keys,
                    },
                }
                await websocket.send(json.dumps(sub_request).encode("utf-8"))

                # --- PHASE 2: Start ATM Radar (cancel stale task on reconnect) ---
                if radar_task is not None:
                    radar_task.cancel()
                radar_task = asyncio.create_task(sync_atm_window(websocket, token_to_instrument))

                # Data-flow watchdog: TCP may stay alive while the upstream feed goes
                # silent. async for / recv() won't raise — so wrap recv() in wait_for
                # and force a reconnect if no messages arrive during market hours.
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=WS_TICK_TIMEOUT_SECS)
                    except asyncio.TimeoutError:
                        now_ist = datetime.now(IST)
                        if is_session_started(now_ist) and is_before(now_ist, 15, 30):
                            console.print(
                                f"[red]WS silent for {WS_TICK_TIMEOUT_SECS}s during market hours. "
                                f"Forcing reconnect.[/red]"
                            )
                            await websocket.close()
                            break
                        continue
                    await process_message_receiver(message, tick_queue, token_to_instrument)

        except websockets.ConnectionClosed as exc:
            console.print(f"[red]WS closed (code={exc.code}). Reconnecting in 5s...[/red]")
            await asyncio.sleep(5)
        except (ConnectionError, OSError) as exc:
            console.print(f"[red]Network error: {exc}. Reconnecting in 5s...[/red]")
            await asyncio.sleep(5)
        except Exception as exc:
            console.print(f"[red]Unexpected: {exc}. Reconnecting in 10s...[/red]")
            await asyncio.sleep(10)


async def dashboard_task(live: Live, log_path: Path) -> None:
    interval = 1.0 / REFRESH_PER_SECOND
    while True:
        live.update(build_dashboard(log_path))
        await asyncio.sleep(interval)


async def mock_ws_task(log_queue: asyncio.Queue) -> None:
    console.print("\n[bold yellow]No usable API token found. Running in mock simulation mode.[/bold yellow]\n")

    # Build baselines from dynamically resolved INSTRUMENT_KEYS
    _mock_prices = {
        "NIFTY": 22500.0, "BANKNIFTY": 48000.0, "USDINR": 93.50,
        "CRUDEOIL": 11500.0, "RELIANCE": 2900.0, "HDFCBANK": 1500.0,
    }
    baselines: dict[str, float] = {}
    for key in INSTRUMENT_KEYS:
        display = DISPLAY_NAMES.get(key, key)
        if display in _mock_prices:
            baselines[key] = _mock_prices[display]

    while True:
        timestamp = time.time()
        for instrument_key, state in symbol_states.items():
            if instrument_key not in baselines: continue
            if "USDINR" in instrument_key: baselines[instrument_key] += random.uniform(-0.03, 0.03)
            else: baselines[instrument_key] += random.uniform(-5.0, 5.0)

            ltp = baselines[instrument_key]
            if instrument_key.startswith("NSE_INDEX|") or instrument_key.startswith("BSE_INDEX|"):
                vtt = 0  # spot indices carry no traded volume
            else:
                vtt = state.last_vtt + random.randint(10, 100)
            record_tick(instrument_key, ltp, vtt, timestamp, log_queue)

        update_mock_pcr(timestamp)
        await asyncio.sleep(0.25)


async def _prefetch_prior_closes(instrument_keys: list[str]) -> None:
    """Fetch the prior trading day's daily close for each key via Upstox historical-candle.
    Populates symbol_states[key].prior_close so gap_pct can be computed at first tick.

    The radar's symbol_states dict is created lazily on first tick — so this helper
    seeds a tiny ephemeral state with just the prior_close, and the live state inherits
    it via reset_runtime_state -> _prior_close_seed lookup at session start.
    """
    today = datetime.now(IST).date()
    # Walk back up to 7 calendar days to find the last weekday with data (handles weekends + holidays).
    start = today - timedelta(days=7)
    end = today - timedelta(days=1)
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for key in instrument_keys:
            if not key:
                continue
            try:
                # Resolve to the numeric token if needed (futures use NSE_FO|<token> already)
                fetch_key = key
                if (not instrument_key_uses_numeric_token(key)
                        and "NSE_INDEX" not in key
                        and "BSE_INDEX" not in key):
                    try:
                        fetch_key = await resolve_historical_instrument_key(session, key)
                    except Exception:
                        fetch_key = key
                url = UPSTOX_HISTORICAL_URL.format(
                    key=quote(fetch_key, safe=""),
                    interval="day",
                    to_date=end.isoformat(),
                    from_date=start.isoformat(),
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        console.print(f"[yellow]prior-close fetch HTTP {resp.status} for {key}[/yellow]")
                        continue
                    payload = await resp.json()
                candles = payload.get("data", {}).get("candles", [])
                if not candles:
                    continue
                # Upstox returns candles sorted newest-first. Index 4 is close.
                candles_sorted = sorted(candles, key=lambda c: c[0], reverse=True)
                prior_close = float(candles_sorted[0][4])
                _prior_close_seed[key] = prior_close
                console.print(f"[green][OK] Prior close {get_display_name(key)}: {prior_close:.2f}[/green]")
            except Exception as exc:
                console.print(f"[yellow]prior-close fetch failed for {key}: {exc}[/yellow]")


# Seed dict — populated by _prefetch_prior_closes at boot, read when SymbolState is initialized
_prior_close_seed: dict[str, float] = {}


async def resolve_dynamic_instruments():
    """Runs on boot. Resolves active expiries and updates all core dictionaries."""
    global PCR_EXPIRY_CODE, INSTRUMENT_KEYS, SUBSCRIPTION_KEYS, PCR_KEYS, PCR_STRIKES
    global PCR_KEY_SIDE, PCR_KEY_STRIKE, DISPLAY_NAMES, FOCUS_PRIORITY
    
    console.print("[cyan]Bootloader: Resolving active contracts from local Master...[/cyan]")
    
    nifty_key, nifty_expiry = await get_active_expiry_key("NIFTY", "OPT")
    crude_key, _ = await get_active_expiry_key("CRUDEOIL", "FUT")
    usdinr_key, _ = await get_active_expiry_key("USDINR", "FUT")
    nifty_fut_key, _ = await get_active_expiry_key("NIFTY", "FUT")
    banknifty_fut_key, _ = await get_active_expiry_key("BANKNIFTY", "FUT")
    sensex_fut_key, _ = await get_active_expiry_key("SENSEX", "FUT")

    # No silent hardcoded fallbacks — master loader already survives network
    # hiccups via disk cache, so reaching here with None means the symbol is
    # genuinely missing from the master. Limping on with a stale hardcoded
    # month rotted every expiry cycle; we fail loud instead so the user fixes
    # the real issue (usually a master format change or network outage).
    missing: list[str] = []
    if nifty_expiry:
        PCR_EXPIRY_CODE = nifty_expiry
        console.print(f"[green][OK] NIFTY Options Expiry: {PCR_EXPIRY_CODE}[/green]")
    else:
        missing.append("NIFTY OPT (weekly/monthly)")

    if crude_key:
        console.print(f"[green][OK] CRUDEOIL Futures: {crude_key}[/green]")
    else:
        missing.append("CRUDEOIL FUT (MCX)")

    if usdinr_key:
        console.print(f"[green][OK] USDINR Futures: {usdinr_key}[/green]")
    else:
        missing.append("USDINR FUT (NSE_FO/NCD_FO)")

    if nifty_fut_key:
        console.print(f"[green][OK] NIFTY Futures (front-month): {nifty_fut_key}[/green]")
    else:
        missing.append("NIFTY FUT (NSE_FO)")

    if banknifty_fut_key:
        console.print(f"[green][OK] BANKNIFTY Futures (front-month): {banknifty_fut_key}[/green]")
    else:
        missing.append("BANKNIFTY FUT (NSE_FO)")

    # SENSEX FUT is non-fatal — Upstox sometimes returns nothing for BSE_FO under
    # certain auth scopes. Warn loudly but let the engine boot without it so
    # NIFTY/BANKNIFTY trading isn't blocked by a BSE outage.
    if sensex_fut_key:
        console.print(f"[green][OK] SENSEX Futures (front-month): {sensex_fut_key}[/green]")
    else:
        console.print("[yellow][WARN] SENSEX FUT not resolvable from master — SENSEX card will run on spot only[/yellow]")

    if missing:
        raise RuntimeError(
            "Cannot resolve active contracts for: " + ", ".join(missing) +
            ". The Upstox instrument master was reached but does not contain a "
            "future-dated contract for these symbols. Check upstream format or "
            "retry later — refusing to subscribe to a stale hardcoded contract."
        )

    # Update core lists with resolved string keys.
    # VIX must be kept here — it drives vix_z_scale() + macro header readout; if it's
    # dropped from this list it never gets subscribed and get_vix_state() returns
    # "awaiting" all session.
    INSTRUMENT_KEYS.clear()
    INSTRUMENT_KEYS.extend([
        "NSE_INDEX|Nifty 50",
        "NSE_INDEX|Nifty Bank",
        SENSEX_INDEX_KEY,
        VIX_INSTRUMENT_KEY,
        nifty_fut_key,
        banknifty_fut_key,
        usdinr_key,
        crude_key,
        "NSE_EQ|RELIANCE",
        "NSE_EQ|HDFCBANK",
    ])
    if sensex_fut_key:
        INSTRUMENT_KEYS.append(sensex_fut_key)

    DISPLAY_NAMES[usdinr_key] = "USDINR"
    DISPLAY_NAMES[crude_key] = "CRUDEOIL"
    DISPLAY_NAMES[nifty_fut_key] = "NIFTY_FUT"
    DISPLAY_NAMES[banknifty_fut_key] = "BN_FUT"
    if sensex_fut_key:
        DISPLAY_NAMES[sensex_fut_key] = "SENSEX_FUT"
    FOCUS_PRIORITY[usdinr_key] = 2
    FOCUS_PRIORITY[crude_key] = 3
    FOCUS_PRIORITY[nifty_fut_key] = 6
    FOCUS_PRIORITY[banknifty_fut_key] = 7
    if sensex_fut_key:
        FOCUS_PRIORITY[sensex_fut_key] = 8

    # Sync USDINR_KEYS with the resolved numeric token. Without this the
    # HTTP bootstrap poll + usdinr_direction() lookup at runtime both fail
    # silently because they search for the hardcoded string key while
    # symbol_states is populated under the resolved exchange-token key.
    USDINR_KEYS.clear()
    USDINR_KEYS.add(usdinr_key)

    # Hide USDINR from the main card grid. WS subscription, macro bias,
    # RBI proximity, and Sonar catalyst checks remain active.
    HIDDEN_FROM_CARDS.add(usdinr_key)

    # Pre-open: fetch prior trading-day daily close for NIFTY/BANKNIFTY spot + futures
    # so gap_pct can be computed at the first tick. Failure is non-fatal — gap_pct stays 0.
    _prior_close_keys = [
        "NSE_INDEX|Nifty 50",
        "NSE_INDEX|Nifty Bank",
        SENSEX_INDEX_KEY,
        nifty_fut_key,
        banknifty_fut_key,
    ]
    if sensex_fut_key:
        _prior_close_keys.append(sensex_fut_key)
    await _prefetch_prior_closes(_prior_close_keys)

    # Fetch current NIFTY LTP for accurate ATM strike at boot
    boot_base_strike = PCR_BASE_STRIKE
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as ltp_session:
            nifty_url = f"https://api.upstox.com/v2/market-quote/ltp?instrument_key={quote('NSE_INDEX|Nifty 50')}"
            async with ltp_session.get(nifty_url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    nifty_ltp = data.get("data", {}).get("NSE_INDEX:Nifty 50", {}).get("last_price", 0)
                    if nifty_ltp > 0:
                        boot_base_strike = int(round(nifty_ltp / PCR_STRIKE_STEP) * PCR_STRIKE_STEP)
                        console.print(f"[green][OK] NIFTY ATM for PCR: {boot_base_strike} (LTP {nifty_ltp:.2f})[/green]")
    except Exception:
        console.print(f"[yellow]! Could not fetch NIFTY LTP, using fallback base strike {PCR_BASE_STRIKE}[/yellow]")

    # Rebuild PCR mapping dynamically
    PCR_KEYS, PCR_STRIKES = generate_pcr_keys(
        boot_base_strike, PCR_WING_COUNT, PCR_STRIKE_STEP, PCR_EXPIRY_CODE
    )
    PCR_KEY_SIDE.clear()
    PCR_KEY_SIDE.update({k: "CE" for k in PCR_KEYS["CE"]})
    PCR_KEY_SIDE.update({k: "PE" for k in PCR_KEYS["PE"]})
    
    PCR_KEY_STRIKE.clear()
    PCR_KEY_STRIKE.update({k: s for k, s in zip(PCR_KEYS["CE"], PCR_STRIKES)})
    PCR_KEY_STRIKE.update({k: s for k, s in zip(PCR_KEYS["PE"], PCR_STRIKES)})

    SUBSCRIPTION_KEYS.clear()
    SUBSCRIPTION_KEYS.extend(list(dict.fromkeys(INSTRUMENT_KEYS + list(PCR_KEY_SIDE))))

    resolved_pcr = [key for key in PCR_KEY_SIDE if resolve_key_from_master(key)]
    resolved_pcr_set = set(resolved_pcr)
    missing_pcr = [key for key in PCR_KEY_SIDE if key not in resolved_pcr_set]
    if missing_pcr:
        console.print(
            f"[bold yellow]! PCR option resolution: {len(resolved_pcr)}/{len(PCR_KEY_SIDE)} keys resolved. "
            f"Missing sample: {', '.join(missing_pcr[:4])}[/bold yellow]"
        )
    else:
        console.print(
            f"[green][OK] PCR option resolution: {len(resolved_pcr)}/{len(PCR_KEY_SIDE)} keys resolved for {PCR_EXPIRY_CODE}[/green]"
        )

    # --- CRITICAL: Update USDINR_KEYS with resolved key ---
    USDINR_KEYS.clear()
    USDINR_KEYS.add(usdinr_key)
    console.print(f"[green][OK] USDINR_KEYS updated: {USDINR_KEYS}[/green]")

    # --- CRITICAL: Rebuild CROSS_ASSET_SUPPRESSION with resolved keys ---
    CROSS_ASSET_SUPPRESSION.clear()
    CROSS_ASSET_SUPPRESSION[crude_key] = ["NSE_INDEX|Nifty 50", "NSE_EQ|RELIANCE"]
    CROSS_ASSET_SUPPRESSION[usdinr_key] = ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]
    CROSS_ASSET_SUPPRESSION["NSE_EQ|HDFCBANK"] = ["NSE_INDEX|Nifty Bank"]
    
    # Also update old April key mappings to May for consistency
    old_april_crude = "MCX_FO|CRUDEOIL26APRFUT"
    old_april_usdinr = "NSE_FO|USDINR26APRFUT"
    if old_april_crude in DISPLAY_NAMES:
        del DISPLAY_NAMES[old_april_crude]
    if old_april_usdinr in DISPLAY_NAMES:
        del DISPLAY_NAMES[old_april_usdinr]

    console.print(f"[green][OK] Cross-asset suppression and USDINR keys updated[/green]")


_last_roll_date: date | None = None


async def expiry_auto_roll() -> None:
    """Daily pre-market check: re-resolve futures/options expiries and hot-swap keys.
    Runs at 08:55 IST every day. If contracts have rolled, refreshes the instrument
    master and rewires all runtime dictionaries — no restart needed.
    """
    global _last_roll_date, _instrument_master_by_symbol

    while True:
        now_ist = datetime.now(IST)
        today = now_ist.date()

        # Only run once per day, at or after 08:55 IST, before market open
        if _last_roll_date == today or now_ist.hour < 8 or (now_ist.hour == 8 and now_ist.minute < 55):
            await asyncio.sleep(60)
            continue

        _last_roll_date = today
        console.print("[cyan]Auto-roll: checking for expiry roll...[/cyan]")

        # Force-refresh instrument master (clear cache so we get fresh data)
        _instrument_master_by_symbol = None

        old_usdinr = next(iter(USDINR_KEYS), None)
        old_crude = next(
            (k for k in INSTRUMENT_KEYS if "CRUDEOIL" in k), None
        )

        try:
            await resolve_dynamic_instruments()
        except Exception as exc:
            console.print(f"[yellow]Auto-roll failed: {exc} — will retry tomorrow[/yellow]")
            await asyncio.sleep(60)
            continue

        new_usdinr = next(iter(USDINR_KEYS), None)
        new_crude = next(
            (k for k in INSTRUMENT_KEYS if "CRUDEOIL" in k), None
        )

        rolled = []
        if old_usdinr and new_usdinr and old_usdinr != new_usdinr:
            rolled.append(f"USDINR: {old_usdinr} → {new_usdinr}")
            # Migrate state to new key
            if old_usdinr in symbol_states:
                symbol_states[new_usdinr] = symbol_states.pop(old_usdinr)
        if old_crude and new_crude and old_crude != new_crude:
            rolled.append(f"CRUDEOIL: {old_crude} → {new_crude}")
            if old_crude in symbol_states:
                symbol_states[new_crude] = symbol_states.pop(old_crude)

        # Ensure new keys exist in symbol_states
        for key in INSTRUMENT_KEYS:
            if key not in symbol_states:
                symbol_states[key] = SymbolState()

        if rolled:
            console.print(f"[green][OK] Auto-roll complete: {', '.join(rolled)}[/green]")
        else:
            console.print("[green][OK] Auto-roll: no expiry change today[/green]")

        await asyncio.sleep(60)


async def run_live_mode() -> None:
    global _fii_snapshot, _sonar_engine

    # --- CRITICAL FIX: Resolve keys BEFORE building symbol_states ---
    await resolve_dynamic_instruments()
    reset_runtime_state()

    # --- PHASE 0: Start async macro worker thread ---
    macro_worker = threading.Thread(target=fetch_macro_worker, daemon=True)
    macro_worker.start()
    console.print("[green][OK] Macro worker thread started[/green]")

    # --- V5.0: Pre-market FII/DII fetch ---
    if FII_BOOT_ENABLED and _FII_AVAILABLE:
        try:
            console.print("[cyan]Fetching FII/DII participant OI...[/cyan]")
            _fii_snapshot = await fetch_latest_fii_data()
            console.print(f"[green][OK] FII/DII loaded: {_fii_snapshot.format_summary()}[/green]")
        except Exception as exc:
            console.print(f"[yellow]FII/DII fetch failed (non-critical): {exc}[/yellow]")

    # --- V5.0: Initialize Sonar news engine ---
    if SONAR_ENABLED and _SONAR_AVAILABLE:
        _sonar_engine = SonarNewsEngine(cooldown_secs=SONAR_COOLDOWN_SECS)
        if _sonar_engine.is_enabled:
            console.print("[green][OK] Sonar news engine initialized[/green]")
        else:
            console.print("[yellow]Sonar disabled: no PERPLEXITY_API_KEY in .env[/yellow]")
            _sonar_engine = None

    log_path = get_log_dir()
    log_queue: asyncio.Queue = asyncio.Queue()
    tick_queue: asyncio.Queue = asyncio.Queue()  # --- PHASE 3: Decoupled Math Pipeline ---
    mock_mode = resolve_mock_mode()

    if not mock_mode and not ACCESS_TOKEN:
        console.print("[bold red]ERROR: Set the UPSTOX_ACCESS_TOKEN environment variable.[/bold red]")
        return

    writer_task = asyncio.create_task(disk_writer_task(log_queue, log_path))

    with Live(
        build_dashboard(log_path), console=console, refresh_per_second=REFRESH_PER_SECOND
    ) as live:
        if mock_mode:
            # Mock mode: no tick_queue, record_tick called directly
            feeder_task = asyncio.create_task(mock_ws_task(log_queue))
            compute_task = None
        else:
            feeder_task = asyncio.create_task(ws_task(log_queue, tick_queue))
            compute_task = asyncio.create_task(compute_worker(tick_queue, log_queue))
            
        ui_task = asyncio.create_task(dashboard_task(live, log_path))
        
        # Auto-roll task: checks for expiry changes daily at 08:55 IST
        roll_task = asyncio.create_task(expiry_auto_roll())

        # CRITICAL FIX: Removed the dead macro_poll_task to stop the crash
        tasks = [writer_task, feeder_task, ui_task, roll_task]
        if compute_task is not None:
            tasks.append(compute_task)

        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            global _GRACEFUL_SHUTDOWN_IN_PROGRESS
            if not _GRACEFUL_SHUTDOWN_IN_PROGRESS:
                _GRACEFUL_SHUTDOWN_IN_PROGRESS = True
                console.print("\n[yellow]Graceful shutdown triggered... flushing data to disk...[/yellow]")
            else:
                console.print("\n[red]Shutdown already in progress. Ignoring second Ctrl+C.[/red]")
        finally:
            # Cancel feed/UI/macro tasks but NOT the writer — it needs to flush
            non_writer_tasks = [t for t in tasks if t is not writer_task]
            for task in non_writer_tasks:
                task.cancel()
            await asyncio.gather(*non_writer_tasks, return_exceptions=True)

            # Signal writer to flush remaining rows and exit
            await log_queue.put(LOG_STOP)
            try:
                await asyncio.wait_for(writer_task, timeout=5.0)
                console.print("\n[green]===============================================[/green]")
                console.print("[green][OK] DATA SAVED. Safe to close terminal.[/green]")
                console.print("[green]===============================================[/green]")
            except asyncio.TimeoutError:
                writer_task.cancel()
                console.print("[red]WARNING: Disk writer timeout. Data may be incomplete.[/red]")


async def run_preflight() -> None:
    console.print("[bold cyan]Initiating Pre-Market API Diagnostics...[/bold cyan]\n")

    if not ACCESS_TOKEN or looks_like_placeholder_token(ACCESS_TOKEN):
        console.print("[bold red][FAIL] Token Check: UPSTOX_ACCESS_TOKEN is missing or invalid.[/bold red]")
        return

    console.print("[bold green][PASS] Token Check: Loaded from .env[/bold green]")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }

    # --- PATCH 4: Local Master Expiry Resolution (zero-latency, no REST API) ---
    console.print("\n[cyan]Testing Local Instrument Master (Dynamic Expiry Resolution)...[/cyan]")
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            await load_upstox_instrument_master(session)
            console.print("[bold green][PASS] Instrument Master: Loaded successfully[/bold green]")
        except Exception as exc:
            console.print(f"[bold red][FAIL] Instrument Master: {exc}[/bold red]")

        for symbol, instrument_type in [("NIFTY", "OPT"), ("CRUDEOIL", "FUT"), ("USDINR", "FUT"), ("NIFTY", "FUT"), ("BANKNIFTY", "FUT")]:
            try:
                key, expiry_code = await get_active_expiry_key(symbol, instrument_type)
                if key and expiry_code:
                    console.print(f"[bold green][PASS] Local Master ({symbol} {instrument_type}): {key} -> Expiry: {expiry_code}[/bold green]")
                elif key:
                    console.print(f"[bold yellow][WARN] Local Master ({symbol} {instrument_type}): Found key {key} but could not extract expiry code[/bold yellow]")
                else:
                    console.print(f"[bold yellow][WARN] Local Master ({symbol} {instrument_type}): No result, will use fallback[/bold yellow]")
            except Exception as exc:
                console.print(f"[bold yellow][WARN] Local Master ({symbol} {instrument_type}): {exc}[/bold yellow]")

    test_symbols = ["NIFTY26MAYFUT", "CRUDEOIL26MAYFUT"]
    exchange_hints = {
        "NIFTY26MAYFUT": "NSE_FO",
        "CRUDEOIL26MAYFUT": "MCX_FO",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        symbol_to_token: dict[str, str] = {}
        token_to_symbol: dict[str, str] = {}

        for symbol in test_symbols:
            try:
                token = await resolve_upstox_token(session, symbol, exchange_hints[symbol])
                symbol_to_token[symbol] = token
                token_to_symbol[token] = symbol
            except Exception as exc:
                console.print(f"[bold red][FAIL] Mapping Check: Could not resolve token for {symbol}: {exc}[/bold red]")

        test_tokens = [symbol_to_token[symbol] for symbol in test_symbols if symbol in symbol_to_token]

        if not test_tokens:
            console.print("[bold red][FAIL] Mapping Check: Could not translate NIFTY/CRUDEOIL. Did the CSVs load?[/bold red]")
            return

        console.print(f"\n[cyan]Testing against tokens: {test_tokens}[/cyan]\n")

        for token in test_tokens:
            quote_url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={token}"
            async with session.get(quote_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get("data", {}).get(token, {}).get("last_price")
                    if price is not None:
                        console.print(f"[bold green][PASS] REST Quote ({token_to_symbol.get(token, token)}): {price}[/bold green]")
                    else:
                        console.print(f"[bold red][FAIL] REST Quote ({token}): Empty price payload.[/bold red]")
                else:
                    console.print(f"[bold red][FAIL] REST Quote ({token}): HTTP {resp.status}[/bold red]")

            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            hist_url = f"https://api.upstox.com/v2/historical-candle/{token}/1minute/{today_str}/{today_str}"
            async with session.get(hist_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candles = data.get("data", {}).get("candles", [])
                    console.print(f"[bold green][PASS] REST Historical ({token_to_symbol.get(token, token)}): {len(candles)} candles fetched.[/bold green]")
                else:
                    console.print(f"[bold yellow][WARN] REST Historical ({token}): HTTP {resp.status} (May be empty if market closed)[/bold yellow]")

    console.print("\n[cyan]Testing WebSocket Snapshot...[/cyan]")
    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        async with websockets.connect(WS_URL, ssl=ssl_ctx, additional_headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}) as websocket:
            sub_request = {
                "guid": "preflight-test",
                "method": "sub",
                "data": {
                    "mode": "full",
                    "instrumentKeys": test_tokens,
                },
            }
            await websocket.send(json.dumps(sub_request).encode("utf-8"))

            for i in range(3):
                try:
                    msg = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue

                feed_resp = pb.FeedResponse()
                feed_resp.ParseFromString(msg)

                if feed_resp.type == pb.market_info:
                    continue

                has_data = False
                for token_key, feed in feed_resp.feeds.items():
                    metrics = extract_feed_metrics(feed)
                    symbol_name = token_to_symbol.get(token_key, token_key)
                    if metrics and metrics[0] > 0.0:
                        has_data = True
                        console.print(f"[bold green][PASS] WS Snapshot ({symbol_name}): LTP = {metrics[0]}[/bold green]")
                    else:
                        console.print(f"[bold red][FAIL] WS Snapshot ({symbol_name}): Empty or 0.0 LTP in fullFeed.[/bold red]")

                if feed_resp.feeds and not has_data:
                    console.print("[bold yellow][WARN] WS returned keys but no valid LTP. Feed requires volume warmup.[/bold yellow]")
                    break
                elif has_data:
                    break

    except Exception as e:
        console.print(f"[bold red][FAIL] WS Connection Error: {e}[/bold red]")

    console.print("\n[bold cyan]Preflight Complete. Exiting.[/bold cyan]")


# --------------------------- REPLAY & BACKTEST MODES ---------------------------

def resolve_replay_symbol(filepath: str) -> tuple[str, str]:
    stem = Path(filepath).stem.upper()
    for key, name in DISPLAY_NAMES.items():
        if name.upper() in stem:
            return key, name
    clean = Path(filepath).stem.replace("_minute", "").replace("_", " ").strip().upper()
    return f"REPLAY|{clean}", clean


async def replay_task(filepath: str, log_queue: asyncio.Queue, replay_key: str) -> None:
    frame = pd.read_csv(filepath)
    col_map = {c.lower(): c for c in frame.columns}
    close_col = col_map.get("close")
    vol_col = col_map.get("volume")
    time_col = col_map.get("time")
    ts_col = col_map.get("datetime") or col_map.get("date") or col_map.get("timestamp")

    if ts_col is None or close_col is None:
        console.print("[bold red]REPLAY ERROR: CSV must have Date/Datetime and Close columns.[/bold red]")
        return

    if time_col is not None and ts_col != time_col:
        frame["_datetime"] = frame[ts_col].astype(str) + " " + frame[time_col].astype(str)
        ts_col = "_datetime"

    frame[ts_col] = parse_replay_datetimes(frame[ts_col])
    if frame[ts_col].dt.tz is None:
        frame[ts_col] = frame[ts_col].dt.tz_localize(IST)

    col_list = list(frame.columns)
    ts_idx = col_list.index(ts_col)
    close_idx = col_list.index(close_col)
    vol_idx = col_list.index(vol_col) if vol_col else None

    cum_vol = 0
    total = len(frame)

    for row in frame.itertuples(index=False):
        ts_dt = row[ts_idx].to_pydatetime()
        ts_unix = ts_dt.timestamp()
        ltp = float(row[close_idx])
        bar_vol = int(row[vol_idx]) if vol_idx is not None else 1
        cum_vol += bar_vol

        record_tick(replay_key, ltp, cum_vol, ts_unix, log_queue, tick_ist=ts_dt)
        await asyncio.sleep(REPLAY_TICK_DELAY)

    state = symbol_states[replay_key]
    state.action_signal = f"REPLAY DONE ({total} bars)"
    state.action_style = "bold green"


async def run_replay_mode(filepath: str) -> None:
    global _replay_mode
    _replay_mode = True

    if not Path(filepath).exists():
        console.print(f"[bold red]File not found: {filepath}[/bold red]")
        return

    replay_key, display_name = resolve_replay_symbol(filepath)
    INSTRUMENT_KEYS.clear()
    INSTRUMENT_KEYS.append(replay_key)
    DISPLAY_NAMES[replay_key] = display_name
    reset_runtime_state()

    log_path = get_log_dir()
    log_queue: asyncio.Queue = asyncio.Queue()

    writer = asyncio.create_task(disk_writer_task(log_queue, log_path))

    with Live(build_dashboard(log_path), console=console, refresh_per_second=REFRESH_PER_SECOND) as live:
        feed = asyncio.create_task(replay_task(filepath, log_queue, replay_key))
        ui = asyncio.create_task(dashboard_task(live, log_path))
        tasks = [feed, ui]

        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await log_queue.put(LOG_STOP)
            await writer


async def fetch_historical_candles(
    session: aiohttp.ClientSession, historical_instrument_key: str, from_date: date, to_date: date, interval: str = "1minute"
) -> list[tuple[datetime, float, int]]:
    encoded_key = quote(historical_instrument_key, safe="")
    all_candles: list[tuple[datetime, float, int]] = []

    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=29), to_date)
        url = UPSTOX_HISTORICAL_URL.format(key=encoded_key, interval=interval, to_date=chunk_end.isoformat(), from_date=chunk_start.isoformat())

        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
            payload = await resp.json()

        candles_raw = payload.get("data", {}).get("candles", [])
        for c in candles_raw:
            ts = datetime.fromisoformat(c[0])
            if ts.tzinfo is None: ts = ts.replace(tzinfo=IST)
            all_candles.append((ts, float(c[4]), int(c[5])))

        chunk_start = chunk_end + timedelta(days=1)

    all_candles.sort(key=lambda x: x[0])
    return all_candles


async def backtest_feed_task(log_queue: asyncio.Queue, merged_candles: list[tuple[datetime, float, int, str]]) -> None:
    cum_vols: dict[str, int] = {}
    last_days: dict[str, date] = {}

    total = len(merged_candles)
    for i, (ts_dt, close, bar_vol, inst_key) in enumerate(merged_candles):
        trading_day = ts_dt.date()

        if last_days.get(inst_key) != trading_day:
            cum_vols[inst_key] = 0
            last_days[inst_key] = trading_day

        cum_vols[inst_key] += bar_vol
        record_tick(inst_key, close, cum_vols[inst_key], ts_dt.timestamp(), log_queue, tick_ist=ts_dt)

        if i % 50 == 0: await asyncio.sleep(0)

    for key in set(c[3] for c in merged_candles):
        if key in symbol_states:
            symbol_states[key].action_signal = f"BACKTEST DONE ({total} bars)"
            symbol_states[key].action_style = "bold green"


async def run_backtest_mode(from_date: date, to_date: date, symbol_filter: list[str] | None = None) -> None:
    global _replay_mode
    _replay_mode = True

    if symbol_filter:
        keys_to_test = []
        for filt in symbol_filter:
            filt_up = filt.strip().upper()
            matched = False
            for key, name in DISPLAY_NAMES.items():
                if name.upper() == filt_up or filt_up in key.upper():
                    keys_to_test.append(key)
                    matched = True
                    break
            if not matched:
                console.print(f"[yellow]Warning: '{filt}' did not match any instrument, skipping.[/yellow]")
        if not keys_to_test:
            console.print("[bold red]No instruments matched the filter.[/bold red]")
            return
    else:
        keys_to_test = list(INSTRUMENT_KEYS)

    INSTRUMENT_KEYS.clear()
    INSTRUMENT_KEYS.extend(keys_to_test)
    reset_runtime_state()

    console.print("[bold cyan]Fetching 1-min candles from Upstox Historical API...[/bold cyan]")
    console.print(f"  Period : {from_date} → {to_date}")
    console.print(f"  Symbols: {', '.join(DISPLAY_NAMES.get(k, k) for k in keys_to_test)}")
    console.print()

    timeout = aiohttp.ClientTimeout(total=60)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {ACCESS_TOKEN}"}
    merged: list[tuple[datetime, float, int, str]] = []

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        resolved_keys: dict[str, str] = {}
        for key in keys_to_test:
            name = DISPLAY_NAMES.get(key, key)
            try:
                resolved_keys[key] = await resolve_historical_instrument_key(session, key)
            except BaseException as exc:
                console.print(f"  [red]x {name}: key resolution failed: {exc}[/red]")

        if not resolved_keys:
            console.print("[bold red]No historical-compatible instrument keys could be resolved.[/bold red]")
            return

        for key, resolved_key in resolved_keys.items():
            name = DISPLAY_NAMES.get(key, key)
            if resolved_key != key:
                console.print(f"  [dim]{name}: historical key {resolved_key}[/dim]")

        console.print()
        fetch_tasks = {
            key: fetch_historical_candles(session, resolved_key, from_date, to_date)
            for key, resolved_key in resolved_keys.items()
        }
        results = dict(zip(fetch_tasks.keys(), await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)))

        for key, result in results.items():
            name = DISPLAY_NAMES.get(key, key)
            if isinstance(result, BaseException):
                console.print(f"  [red]x {name}: {result}[/red]")
                continue
            candles: list[tuple[datetime, float, int]] = result
            console.print(f"  [green][OK] {name}: {len(candles)} candles[/green]")
            merged.extend((ts, close, vol, key) for ts, close, vol in candles)

    if not merged:
        console.print("[bold red]No data fetched. Check your API key and date range.[/bold red]")
        return

    merged.sort(key=lambda x: x[0])
    trading_days = len({c[0].date() for c in merged})
    console.print(f"\n[bold green]Total: {len(merged)} candles across {trading_days} trading day(s). Starting backtest...[/bold green]\n")

    log_path = get_log_path()
    log_queue: asyncio.Queue = asyncio.Queue()
    writer = asyncio.create_task(disk_writer_task(log_queue, log_path))

    with Live(build_dashboard(log_path), console=console, refresh_per_second=REFRESH_PER_SECOND) as live:
        feed = asyncio.create_task(backtest_feed_task(log_queue, merged))
        ui = asyncio.create_task(dashboard_task(live, log_path))
        tasks = [feed, ui]

        try: await asyncio.gather(*tasks)
        finally:
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await log_queue.put(LOG_STOP)
            await writer

    console.print(f"\n[bold cyan]Backtest complete. Log saved to {log_path}[/bold cyan]")
    console.print("[dim]Run --review to analyze signals.[/dim]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GammaLeak Engine")
    parser.add_argument("--review", nargs="?", const="today", metavar="YYYY-MM-DD", help="Review today's CSV.")
    parser.add_argument("--min-z", type=float, default=2.0, help="Minimum abs z-score to grade.")
    parser.add_argument("--window", type=int, default=REVIEW_DEFAULT_WINDOW_MINUTES, help="Forward review window (min).")
    parser.add_argument("--entry-mode", choices=REVIEW_ENTRY_MODES, default=REVIEW_ENTRY_TOUCH, help="Review logic.")
    parser.add_argument("--min-favorable-pts", type=float, default=None, help="Override per-symbol favorable-move threshold (points). If unset, MIN_FAVORABLE_POINTS_PER_SYMBOL from config is used.")
    parser.add_argument("--replay", metavar="FILE", help="Replay offline CSV backtest.")
    parser.add_argument("--backtest", action="store_true", help="Fetch Upstox 1-min candles.")
    parser.add_argument("--preflight", action="store_true", help="Run pre-market diagnostics.")
    parser.add_argument("--from-date", metavar="YYYY-MM-DD", help="Backtest start date.")
    parser.add_argument("--to-date", metavar="YYYY-MM-DD", help="Backtest end date.")
    parser.add_argument("--symbols", metavar="SYM", nargs="+", help="Backtest specific symbols.")
    return parser.parse_args()

def cli() -> int:
    global ACCESS_TOKEN
    args = parse_args()

    current_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    
    if not args.preflight and not args.review and not args.replay and not args.backtest:
        needs_refresh = (not current_token or looks_like_placeholder_token(current_token) or is_token_expired(current_token))
        if needs_refresh:
            reason = "expired" if current_token and not looks_like_placeholder_token(current_token) else "missing/invalid"
            console.print(f"[yellow]Token {reason}. Starting OAuth2 refresh...[/yellow]")
            try:
                from oauth_token_exchange import get_fresh_access_token
                new_token = get_fresh_access_token()
                if new_token:
                    ACCESS_TOKEN = new_token
                    os.environ["UPSTOX_ACCESS_TOKEN"] = new_token
                    console.print("[green][OK] Token refreshed successfully[/green]")
                else:
                    console.print("[red]Failed to refresh token. Exiting.[/red]")
                    return 1
            except ImportError:
                console.print("[red]ERROR: oauth_token_exchange module not found[/red]")
                return 1
            except Exception as e:
                console.print(f"[red]Token refresh error: {e}[/red]")
                return 1

    if args.preflight:
        asyncio.run(run_preflight())
        return 0

    if args.review is not None:
        review_day = datetime.now(IST).date() if args.review == "today" else parse_review_date(args.review)
        return run_review(review_day, args.min_z, args.window, args.entry_mode, args.min_favorable_pts)

    if args.replay is not None:
        asyncio.run(run_replay_mode(args.replay))
        return 0

    if args.backtest:
        if not ACCESS_TOKEN or looks_like_placeholder_token(ACCESS_TOKEN) or is_token_expired(ACCESS_TOKEN):
            console.print("[bold red]ERROR: --backtest requires a valid (non-expired) UPSTOX_ACCESS_TOKEN in .env[/bold red]")
            return 1
        today = datetime.now(IST).date()
        bt_to = date.fromisoformat(args.to_date) if args.to_date else today
        bt_from = date.fromisoformat(args.from_date) if args.from_date else bt_to - timedelta(days=7)
        asyncio.run(run_backtest_mode(bt_from, bt_to, args.symbols))
        return 0

    asyncio.run(run_live_mode())
    return 0

if __name__ == "__main__":
    import signal
    
    shutdown_initiated = False
    
    def signal_handler(signum, frame):
        global shutdown_initiated
        if shutdown_initiated: return
        shutdown_initiated = True
        console.print("\n[yellow]Shutting down gracefully (ignoring further Ctrl+C)...[/yellow]")
        raise KeyboardInterrupt()
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        raise SystemExit(cli())
    except KeyboardInterrupt:
        console.print("[green]GammaLeak stopped.[/green]")
