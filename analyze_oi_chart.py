"""Retroactive analysis of the OI Flow Anchored Velocity Chart.

Joins three data sources from one session:
  - logs/<date>_events.csv     (engine sig_state transitions / CONFIRMs)
  - logs/<date>_oi_state.csv   (5-sec snapshots of chart state: walls,
                                max-pain, CE/PE delta velocities, spot, fut)
  - logs/<date>/<symbol>.csv   (per-tick price stream)

Produces four diagnostic reports:

  1. ANCHOR TOUCH ANALYSIS -- for each chart anchor (PE wall = floor,
     CE wall = ceiling, max-pain = magnet), find every moment NIFTY spot
     came within `--proximity` points of it. Tag each touch by what
     happened in the next 5/15 min -- bounce away, breach through, or
     drift. Reports per-anchor "respect rate".

  2. CE/PE DELTA SPIKE ANALYSIS -- find moments the CE_delta or PE_delta
     velocity line spiked beyond N standard deviations of its rolling
     mean. Measure the 15-min forward NIFTY move from each spike. Did
     PE-writers-in (PE_delta spike +) actually precede a bounce up?

  3. CONFIRM CONTEXT -- for every engine CONFIRM on NIFTY/NIFTY_FUT, look
     up the contemporaneous chart snapshot, compute distance to nearest
     anchor at fire time, and bucket the per-bucket accuracy. Tests
     whether CONFIRMs near a wall outperform mid-range CONFIRMs.

  4. BASIS DIVERGENCE -- rolling fut-spot basis. When basis widens
     unusually, does spot follow fut?

Usage:
    python analyze_oi_chart.py                       # today
    python analyze_oi_chart.py --date 2026-05-20
    python analyze_oi_chart.py --date 2026-05-20 --proximity 15
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date as date_t, datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Repo-relative imports
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import logging
logging.disable(logging.CRITICAL)

from core.config import (
    IST, MFE_ATR_K, MFE_ATR_PROXY_WINDOW_SECS,
    MIN_FAVORABLE_POINTS_PER_SYMBOL,
)
from gammaleak_runtime.io_logs import (
    get_events_log_path, get_log_dir, get_oi_state_log_path,
)


# --------------------------- helpers ---------------------------

def hr(ch: str = "-", n: int = 78) -> None:
    print(ch * n)


def section(title: str) -> None:
    print()
    hr("=")
    print(title)
    hr("=")


def fmt_pts(x: float | None, w: int = 8) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return f"{'-':>{w}s}"
    return f"{x:>{w}.2f}"


def load_inputs(day: date_t) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (events, oi_state, nifty_ticks, nifty_fut_ticks). Each may be empty."""
    ev_path = get_events_log_path(day)
    oi_path = get_oi_state_log_path(day)
    log_dir = get_log_dir(day)

    events = pd.read_csv(ev_path) if ev_path.exists() else pd.DataFrame()
    oi_state = pd.read_csv(oi_path) if oi_path.exists() else pd.DataFrame()

    def load_ticks(symbol: str) -> pd.DataFrame:
        p = log_dir / f"{symbol}.csv"
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_csv(p)
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df["ltp"] = pd.to_numeric(df["ltp"], errors="coerce")
        return df.dropna(subset=["timestamp", "ltp"]).sort_values("timestamp").reset_index(drop=True)

    nifty = load_ticks("NIFTY")
    nifty_fut = load_ticks("NIFTY_FUT")

    if not oi_state.empty:
        oi_state = oi_state.copy()
        oi_state["timestamp"] = pd.to_numeric(oi_state["timestamp"], errors="coerce")
        for col in ("spot", "fut", "top_ce_strike", "top_pe_strike",
                    "max_pain", "ce_delta", "pe_delta"):
            if col in oi_state.columns:
                oi_state[col] = pd.to_numeric(oi_state[col], errors="coerce")
        oi_state = oi_state.dropna(subset=["timestamp", "spot"]).sort_values("timestamp").reset_index(drop=True)

    if not events.empty:
        events = events.copy()
        events["timestamp"] = pd.to_numeric(events["timestamp"], errors="coerce")
        for col in ("z_score", "ltp", "side", "conviction"):
            if col in events.columns:
                events[col] = pd.to_numeric(events[col], errors="coerce")
        events = events.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    return events, oi_state, nifty, nifty_fut


def nearest_tick_price(ticks: pd.DataFrame, ts: float) -> float | None:
    if ticks.empty:
        return None
    arr = ticks["timestamp"].to_numpy()
    idx = int(np.searchsorted(arr, ts, side="right") - 1)
    if idx < 0:
        return None
    return float(ticks["ltp"].iloc[idx])


def forward_move(ticks: pd.DataFrame, ts: float, window_secs: int) -> tuple[float, float] | None:
    """Returns (mfe_long, mfe_short) -- max favorable for each side over the
    forward window, relative to price at ts. Caller picks the relevant one.
    """
    if ticks.empty:
        return None
    start = float(np.searchsorted(ticks["timestamp"].to_numpy(), ts, side="left"))
    if start >= len(ticks):
        return None
    end_ts = ts + window_secs
    end = int(np.searchsorted(ticks["timestamp"].to_numpy(), end_ts, side="right"))
    if end <= start:
        return None
    slice_ = ticks.iloc[int(start):end]
    if slice_.empty:
        return None
    entry = float(slice_["ltp"].iloc[0])
    mfe_long = float(slice_["ltp"].max()) - entry      # move UP
    mfe_short = entry - float(slice_["ltp"].min())     # move DOWN
    return max(0.0, mfe_long), max(0.0, mfe_short)


# --------------------------- section 1: anchor touches ---------------------------

def analyze_anchor_touches(
    oi_state: pd.DataFrame, ticks: pd.DataFrame, proximity_pts: float,
    window_short_secs: int = 300, window_long_secs: int = 900,
) -> None:
    section("1. ANCHOR TOUCH ANALYSIS")
    print(f"Proximity band: {proximity_pts} pts | forward windows: {window_short_secs//60}m + {window_long_secs//60}m")
    print()

    if oi_state.empty:
        print("[no oi_state.csv for this date -- engine wasn't running with the OI persister yet]")
        return
    if ticks.empty:
        print("[no NIFTY per-tick log available]")
        return

    # For each anchor type, walk oi_state. A "touch" event opens when spot
    # comes within `proximity_pts` of the anchor; closes when spot moves
    # > 2 × proximity away. We grade each touch by the 5m/15m forward move
    # at the END of the touch (i.e., the exit price), classifying the move
    # direction relative to the anchor type.
    anchor_specs = [
        ("PE Wall (Floor)",  "top_pe_strike", "bounce_up"),
        ("CE Wall (Ceiling)","top_ce_strike", "bounce_down"),
        ("Max Pain (Magnet)","max_pain",      "drift_toward"),
    ]

    for label, col, expected in anchor_specs:
        if col not in oi_state.columns:
            continue
        active = False
        touch_start_ts = None
        touch_anchor = None
        events_list = []
        for row in oi_state.itertuples(index=False):
            anchor = float(getattr(row, col))
            if anchor <= 0:
                continue
            dist = abs(float(row.spot) - anchor)
            if not active and dist <= proximity_pts:
                active = True
                touch_start_ts = float(row.timestamp)
                touch_anchor = anchor
            elif active and dist > 2 * proximity_pts:
                # Touch ended.
                events_list.append((touch_start_ts, float(row.timestamp), touch_anchor, float(row.spot)))
                active = False
                touch_start_ts = touch_anchor = None
        # Close any still-open touch at end of file
        if active and touch_start_ts is not None:
            last_row = oi_state.iloc[-1]
            events_list.append((touch_start_ts, float(last_row["timestamp"]),
                                touch_anchor, float(last_row["spot"])))

        # Grade outcomes
        n_total = len(events_list)
        n_respected_5m = n_respected_15m = 0
        for start_ts, end_ts, anchor, exit_spot in events_list:
            entry = exit_spot
            fwd_short = forward_move(ticks, end_ts, window_short_secs)
            fwd_long = forward_move(ticks, end_ts, window_long_secs)
            if fwd_short is None or fwd_long is None:
                continue
            ml5, ms5 = fwd_short
            ml15, ms15 = fwd_long
            if expected == "bounce_up":   # PE wall = floor, expect price up
                if ml5 >= proximity_pts: n_respected_5m += 1
                if ml15 >= proximity_pts: n_respected_15m += 1
            elif expected == "bounce_down":  # CE wall = ceiling, expect price down
                if ms5 >= proximity_pts: n_respected_5m += 1
                if ms15 >= proximity_pts: n_respected_15m += 1
            else:  # max-pain magnet -- direction toward anchor is the "respect"
                drift_toward = (exit_spot < anchor and ml5 >= proximity_pts) or \
                               (exit_spot > anchor and ms5 >= proximity_pts)
                if drift_toward: n_respected_5m += 1
                drift_toward_15 = (exit_spot < anchor and ml15 >= proximity_pts) or \
                                   (exit_spot > anchor and ms15 >= proximity_pts)
                if drift_toward_15: n_respected_15m += 1

        if n_total == 0:
            print(f"{label:24s}  no touches recorded")
            continue
        r5 = n_respected_5m / n_total * 100
        r15 = n_respected_15m / n_total * 100
        print(f"{label:24s}  touches={n_total:>3d}   "
              f"respected_5m={n_respected_5m:>3d} ({r5:5.1f}%)   "
              f"respected_15m={n_respected_15m:>3d} ({r15:5.1f}%)")


# --------------------------- section 2: delta spikes ---------------------------

def analyze_delta_spikes(
    oi_state: pd.DataFrame, ticks: pd.DataFrame,
    rolling_window: int = 360, z_threshold: float = 2.0,
    window_secs: int = 900,
) -> None:
    section("2. CE/PE DELTA VELOCITY SPIKE ANALYSIS")
    print(f"Spike = |delta - rolling_mean({rolling_window*5}s)| > {z_threshold} * rolling_std")
    print(f"Forward window: {window_secs//60}m")
    print()

    if oi_state.empty:
        print("[no oi_state.csv]")
        return
    if ticks.empty:
        print("[no NIFTY per-tick log]")
        return

    for delta_col, label, favorable_dir in (
        ("ce_delta", "CE writers IN (CE_delta +)", "down"),
        ("pe_delta", "PE writers IN (PE_delta +)", "up"),
    ):
        if delta_col not in oi_state.columns:
            continue
        series = oi_state[delta_col].astype(float)
        rolling = series.rolling(rolling_window, min_periods=12)
        z = (series - rolling.mean()) / rolling.std().replace(0, np.nan)
        # Positive spikes: writers adding inventory
        spike_idx = oi_state.index[(z > z_threshold) & (series > 0)].tolist()
        if not spike_idx:
            print(f"{label:32s}  no positive spikes")
            continue

        n_total = n_favorable = 0
        avg_fwd = 0.0
        for i in spike_idx:
            ts = float(oi_state["timestamp"].iloc[i])
            fwd = forward_move(ticks, ts, window_secs)
            if fwd is None:
                continue
            ml, ms = fwd
            n_total += 1
            if favorable_dir == "up":
                avg_fwd += ml
                if ml > ms: n_favorable += 1
            else:
                avg_fwd += ms
                if ms > ml: n_favorable += 1
        if n_total == 0:
            print(f"{label:32s}  spikes={len(spike_idx)} but no forward data")
            continue
        avg_fwd /= n_total
        rate = n_favorable / n_total * 100
        print(f"{label:32s}  spikes={n_total:>3d}   "
              f"correct_dir={n_favorable:>3d} ({rate:5.1f}%)   "
              f"avg_favorable_fwd_move={avg_fwd:5.2f}pts")


# --------------------------- section 3: CONFIRM context ---------------------------

def analyze_confirm_context(
    events: pd.DataFrame, oi_state: pd.DataFrame,
    nifty_ticks: pd.DataFrame, nifty_fut_ticks: pd.DataFrame,
    window_secs: int = 900,
) -> None:
    section("3. CONFIRM CONTEXT (anchor proximity at fire time -> outcome)")

    if events.empty:
        print("[no events.csv]")
        return
    if oi_state.empty:
        print("[no oi_state.csv -- anchor context unavailable]")
        return

    confirms = events[(events["event_type"] == "CONFIRM") &
                       (events["symbol"].isin(["NIFTY", "NIFTY_FUT"]))].copy()
    if confirms.empty:
        print("[no NIFTY/NIFTY_FUT CONFIRM events]")
        return

    oi_ts_arr = oi_state["timestamp"].to_numpy()
    print(f"NIFTY/NIFTY_FUT CONFIRMs analyzed: {len(confirms)}")
    print()
    print(f"{'time':<19s} {'symbol':<10s} {'side':>4s} {'spot':>8s} "
          f"{'PE_wall':>8s} {'CE_wall':>8s} {'maxpain':>8s} {'nearest':>10s} "
          f"{'MFE':>7s} {'result':<8s}")
    hr("-")

    bucket_near = defaultdict(lambda: {"n": 0, "moved": 0})
    for r in confirms.itertuples(index=False):
        ts = float(r.timestamp)
        idx = int(np.searchsorted(oi_ts_arr, ts, side="right") - 1)
        if idx < 0 or idx >= len(oi_state):
            continue
        snap = oi_state.iloc[idx]
        spot = float(snap["spot"])
        pe_wall = float(snap.get("top_pe_strike", 0))
        ce_wall = float(snap.get("top_ce_strike", 0))
        mp = float(snap.get("max_pain", 0))

        anchors = []
        if pe_wall > 0: anchors.append(("PE", pe_wall))
        if ce_wall > 0: anchors.append(("CE", ce_wall))
        if mp > 0:      anchors.append(("MP", mp))
        if not anchors:
            continue
        nearest_label, nearest_strike = min(anchors, key=lambda kv: abs(spot - kv[1]))
        nearest_dist = abs(spot - nearest_strike)
        nearest_str = f"{nearest_label}@{nearest_dist:.0f}"

        # Forward move along the signal's side
        ticks = nifty_fut_ticks if r.symbol == "NIFTY_FUT" else nifty_ticks
        fwd = forward_move(ticks, ts, window_secs)
        if fwd is None:
            continue
        ml, ms = fwd
        mfe = ml if int(r.side) < 0 else ms
        floor = MIN_FAVORABLE_POINTS_PER_SYMBOL.get(r.symbol, 15.0)
        result = "OK MOVED" if mfe >= floor else "X WEAK"

        time_str = str(r.timestamp_ist)[:19]
        print(f"{time_str:<19s} {r.symbol:<10s} {int(r.side):>+4d} {spot:>8.2f} "
              f"{pe_wall:>8.0f} {ce_wall:>8.0f} {mp:>8.0f} {nearest_str:>10s} "
              f"{mfe:>7.2f} {result:<8s}")

        # Bucket: near wall (<20pts), mid (<60pts), far (>=60pts)
        if nearest_dist < 20:
            key = "near-wall (<20pts)"
        elif nearest_dist < 60:
            key = "mid (20-60pts)"
        else:
            key = "far (>=60pts)"
        bucket_near[key]["n"] += 1
        if result == "OK MOVED":
            bucket_near[key]["moved"] += 1

    print()
    print("Bucket by anchor proximity at fire time:")
    for key in ("near-wall (<20pts)", "mid (20-60pts)", "far (>=60pts)"):
        b = bucket_near.get(key, {"n": 0, "moved": 0})
        if b["n"] == 0:
            print(f"  {key:24s}  n=0")
            continue
        acc = b["moved"] / b["n"] * 100
        print(f"  {key:24s}  n={b['n']:>3d}  moved={b['moved']:>3d}  acc={acc:5.1f}%")


# --------------------------- section 4: basis divergence ---------------------------

def analyze_basis_divergence(oi_state: pd.DataFrame, ticks: pd.DataFrame,
                              window_secs: int = 600) -> None:
    section("4. BASIS (FUT - SPOT) DIVERGENCE")

    if oi_state.empty or ticks.empty:
        print("[insufficient data]")
        return
    if "fut" not in oi_state.columns:
        print("[no fut column]")
        return

    basis = oi_state["fut"] - oi_state["spot"]
    basis = basis.where(oi_state["fut"] > 0)
    print(f"Basis stats:  mean={basis.mean():.2f}  std={basis.std():.2f}  "
          f"min={basis.min():.2f}  max={basis.max():.2f}  n={basis.notna().sum()}")

    if basis.std() == 0 or np.isnan(basis.std()):
        print("[basis flat -- no divergence to analyze]")
        return

    z = (basis - basis.rolling(180, min_periods=24).mean()) / basis.rolling(180, min_periods=24).std()
    widen_idx = oi_state.index[z.abs() > 2].tolist()
    if not widen_idx:
        print("[no basis divergence spikes (|z|>2) found]")
        return

    n_total = n_correct = 0
    for i in widen_idx:
        ts = float(oi_state["timestamp"].iloc[i])
        b = float(basis.iloc[i])
        fwd = forward_move(ticks, ts, window_secs)
        if fwd is None:
            continue
        ml, ms = fwd
        n_total += 1
        # Hypothesis: positive basis (fut > spot) -> spot rises to catch up
        if b > 0 and ml > ms: n_correct += 1
        elif b < 0 and ms > ml: n_correct += 1
    if n_total == 0:
        print("[no graded basis events]")
        return
    rate = n_correct / n_total * 100
    print(f"Basis spike -> spot followed direction: {n_correct}/{n_total} = {rate:.1f}%")


# --------------------------- main ---------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")
    p.add_argument("--proximity", type=float, default=10.0,
                   help="Anchor touch proximity band in points (default 10)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.date:
        try:
            day = date_t.fromisoformat(args.date)
        except ValueError:
            print(f"Bad date: {args.date}")
            return 1
    else:
        day = datetime.now(IST).date()

    print(f"OI CHART RETROSPECTIVE -- {day.isoformat()}")
    hr("=")

    events, oi_state, nifty, nifty_fut = load_inputs(day)
    print(f"events.csv      rows: {len(events):,}    (CONFIRMs: {(events.get('event_type', pd.Series(dtype=str)) == 'CONFIRM').sum() if not events.empty else 0})")
    print(f"oi_state.csv    rows: {len(oi_state):,}    (~{len(oi_state)*5/60:.1f} min covered)")
    print(f"NIFTY ticks         : {len(nifty):,}")
    print(f"NIFTY_FUT ticks     : {len(nifty_fut):,}")

    if oi_state.empty and events.empty:
        print("\nNothing to analyze -- both events.csv and oi_state.csv are missing.")
        return 1

    analyze_anchor_touches(oi_state, nifty, proximity_pts=args.proximity)
    analyze_delta_spikes(oi_state, nifty)
    analyze_confirm_context(events, oi_state, nifty, nifty_fut)
    analyze_basis_divergence(oi_state, nifty)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
