"""Tick-log integrity census — quantifies feed gaps in logs/<date>/<SYM>.csv
and, critically, how many graded CONFIRMs have gaps inside their 10-minute
forward MFE window (i.e. whether calibrate.py's hit rates are trustworthy).

Session hours 09:15–15:30 IST. A "gap" is an inter-tick interval > GAP_SECS
inside those hours. Coverage = 1 − dead_time/session_span.

Outputs:
  1. per-session worst offenders (dead time, max gap, coverage)
  2. per-symbol aggregate coverage
  3. CONFIRM grading-window damage: % of graded fires whose forward window
     (fire_ts .. fire_ts+600s) contains a gap > GAP_SECS, per session.

Usage: python research/data_integrity_census.py [--gap-secs 60]
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = "logs"
FWD_WINDOW_SECS = 600.0


def session_bounds(date_str: str) -> tuple[float, float]:
    d = datetime.fromisoformat(date_str).replace(tzinfo=IST)
    return (d.replace(hour=9, minute=15).timestamp(),
            d.replace(hour=15, minute=30).timestamp())


def load_ts(path: str) -> np.ndarray:
    ts = []
    try:
        with open(path, newline="") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                try:
                    ts.append(float(row[0]))
                except (ValueError, IndexError):
                    continue
    except OSError:
        pass
    return np.asarray(ts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap-secs", type=float, default=60.0)
    args = ap.parse_args()
    G = args.gap_secs

    day_dirs = sorted(d for d in glob.glob(os.path.join(LOG_DIR, "????-??-??"))
                      if os.path.isdir(d))
    sess_stats = []          # (date, sym, coverage, dead, max_gap, n_ticks, gaps[])
    gaps_by_key: dict[tuple[str, str], np.ndarray] = {}

    for dd in day_dirs:
        date_str = os.path.basename(dd)
        so, sc = session_bounds(date_str)
        for path in sorted(glob.glob(os.path.join(dd, "*.csv"))):
            stem = os.path.splitext(os.path.basename(path))[0]
            if "." in stem or stem == "VIX":
                continue  # archived schema files / display-only
            ts = load_ts(path)
            ts = ts[(ts >= so) & (ts <= sc)]
            if ts.size < 50:
                continue
            ts.sort()
            d = np.diff(ts)
            # include the head/tail dead zones (feed started late / died early)
            head = max(0.0, ts[0] - so)
            tail = max(0.0, sc - ts[-1])
            gap_mask = d > G
            dead = float(d[gap_mask].sum()) + (head if head > G else 0) + (tail if tail > G else 0)
            span = sc - so
            cov = 1.0 - dead / span
            mx = float(d.max()) if d.size else 0.0
            sess_stats.append((date_str, stem, cov, dead, max(mx, head, tail), ts.size))
            # store gap intervals for the confirm-window check
            starts = ts[:-1][gap_mask]
            ends = ts[1:][gap_mask]
            if head > G:
                starts = np.append(so, starts); ends = np.append(ts[0], ends)
            if tail > G:
                starts = np.append(starts, ts[-1]); ends = np.append(ends, sc)
            gaps_by_key[(date_str, stem)] = np.column_stack([starts, ends]) if starts.size else np.empty((0, 2))

    print(f"CENSUS — {len(day_dirs)} session dirs, {len(sess_stats)} symbol-sessions, gap>{G:.0f}s\n")

    print("WORST 15 symbol-sessions by dead time:")
    print(f"  {'date':<12}{'symbol':<14}{'coverage':>9}{'dead_min':>9}{'max_gap_min':>12}{'ticks':>8}")
    for date_str, sym, cov, dead, mx, n in sorted(sess_stats, key=lambda x: -x[3])[:15]:
        print(f"  {date_str:<12}{sym:<14}{cov*100:>8.1f}%{dead/60:>9.1f}{mx/60:>12.1f}{n:>8}")

    by_sym = defaultdict(list)
    for _, sym, cov, *_ in sess_stats:
        by_sym[sym].append(cov)
    print("\nPER-SYMBOL mean coverage (all sessions):")
    for sym in sorted(by_sym, key=lambda s: np.mean(by_sym[s])):
        c = np.array(by_sym[sym])
        print(f"  {sym:<14} mean {c.mean()*100:5.1f}%   worst {c.min()*100:5.1f}%   sessions {c.size}")

    # ---- CONFIRM grading-window damage ----
    n_conf = n_damaged = 0
    dmg_by_day = defaultdict(lambda: [0, 0])
    for fpath in sorted(glob.glob(os.path.join(LOG_DIR, "*_events.csv"))):
        m = re.search(r"(\d{4}-\d{2}-\d{2})_events\.csv$", os.path.basename(fpath))
        if not m:
            continue
        date_str = m.group(1)
        with open(fpath, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("event_type") != "CONFIRM":
                    continue
                setup = (row.get("setup_label") or "").strip()
                if not setup or setup.startswith("REGIME_BLOCK"):
                    continue
                try:
                    fire = float(row.get("timestamp") or 0)
                except ValueError:
                    continue
                sym = row.get("symbol", "")
                g = gaps_by_key.get((date_str, sym))
                if g is None:
                    continue
                n_conf += 1
                dmg_by_day[date_str][1] += 1
                # any gap overlapping [fire, fire+600]?
                if g.size and bool(np.any((g[:, 0] < fire + FWD_WINDOW_SECS) & (g[:, 1] > fire))):
                    n_damaged += 1
                    dmg_by_day[date_str][0] += 1

    print(f"\nCONFIRM GRADING-WINDOW DAMAGE (gap>{G:.0f}s inside fire..+10min):")
    print(f"  {n_damaged}/{n_conf} graded CONFIRMs have a feed gap in their forward window"
          f" = {n_damaged/max(1,n_conf)*100:.1f}%")
    bad_days = {d: v for d, v in dmg_by_day.items() if v[0] > 0}
    if bad_days:
        print("  by session (damaged/total):")
        for d in sorted(bad_days):
            dmg, tot = bad_days[d]
            print(f"    {d}: {dmg}/{tot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
