"""Engine-wide module-level state singletons.

Only state that is SHARED across packages (engine, ui/serializers, future
signals/orderflow modules) lives here. Engine-internal state with rebind
semantics (replay mode flags, FII snapshot, Sonar engine handle, OI flow
last-sample timestamp) stays in GammaLeak.py because Python rebinds
don't propagate across modules.

Mutation is fine (`symbol_states[key] = ...`, `pcr_state.ce_oi.clear()`,
`oi_flow_timeline.append(...)`) — every consumer holds a reference to the
same underlying object.
"""
from __future__ import annotations

from collections import deque

from rich.console import Console

from core.config import (
    OI_FLOW_TIMELINE_WINDOW_SECS,
    OI_FLOW_TIMELINE_SAMPLE_SECS,
)
from core.models import (
    SymbolState,
    MacroState,
    PCRState,
    IndexDriverState,
    OILevelsState,
)


# instrument_key → human-readable display name. Seeded with hardcoded keys at
# module load so the engine can boot before the instrument master loads;
# bootloader mutates this in place (`DISPLAY_NAMES[k] = "NIFTY_FUT"`, etc.)
# once expiry-coded keys are resolved.
DISPLAY_NAMES: dict[str, str] = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "NSE_INDEX|Nifty Bank": "BANKNIFTY",
    "BSE_INDEX|SENSEX": "SENSEX",
    "NSE_INDEX|India VIX": "VIX",
    "NSE_FO|USDINR26MAYFUT": "USDINR",
    "MCX_FO|CRUDEOIL26MAYFUT": "CRUDEOIL",
    "NSE_EQ|RELIANCE": "RELIANCE",
    "NSE_EQ|HDFCBANK": "HDFCBANK",
}

# Per-instrument tick + math state. Keyed by Upstox instrument_key
# (e.g., "NSE_INDEX|Nifty 50"). Populated by the WS feed handler on first tick.
symbol_states: dict[str, SymbolState] = {}

# Macro context (USDINR / PCR / etc.) — mutated in place by the macro poller.
macro_state = MacroState()

# Per-strike CE/PE OI snapshots + rolling histories for OI flow classification.
pcr_state = PCRState()

# Pairwise correlation + lead-lag between indices and their heavyweight constituents.
index_driver_state = IndexDriverState()

# Max-pain + gamma walls per symbol. Refreshed by refresh_oi_levels on its own cadence.
oi_levels_state = OILevelsState()

# Bounded ring buffer powering the anchored velocity chart on the dashboard.
# Capacity = window / sample, so 30 min @ 5s = 360 rows max.
oi_flow_timeline: deque = deque(
    maxlen=OI_FLOW_TIMELINE_WINDOW_SECS // OI_FLOW_TIMELINE_SAMPLE_SECS
)

# Shared Rich console — terminal output for both the engine dashboard and
# disk_writer_task. Singleton because Rich's color/cursor state is global.
console = Console()
