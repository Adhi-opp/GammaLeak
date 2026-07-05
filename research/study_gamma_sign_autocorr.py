"""A4 go/no-go study: does the participant-OI dealer-gamma sign predict the
intraday momentum-vs-reversion regime? (Proposal P2, validation layer.)

Pre-registered design (2026-07-04, before results were seen):

  Conditioning variable  dealer_short_gamma_score from data/participant_history.csv,
                         LAGGED one row (score published evening T-1 conditions day T
                         — publication-lag discipline, no leakage).
  Primary metric         lag-1 autocorrelation of 5-min NIFTY returns within day T.
  Secondary metric       trendiness |close-open| / (high-low) of day T.
  Tests                  (a) tercile split of score -> mean AC + trendiness per
                         tercile; (b) Spearman rank corr(score, AC); (c) month-block
                         bootstrap CI on (top-tercile AC - bottom-tercile AC).
  GO criterion           top-minus-bottom tercile AC difference > 0 with 90% block-
                         bootstrap CI excluding 0, sign consistent with hypothesis
                         (more dealer-short-gamma -> more momentum).
  NO-GO                  CI straddles 0 or sign inverts -> Phase C intraday sign
                         evolution deprioritized; anchor stays daily context only.

1-min candles fetched via backtest_api.fetch_candles in month chunks, cached
under historical/nifty1m/. Idempotent; re-runs cost nothing.

Usage:  python research/study_gamma_sign_autocorr.py [--from 2023-08-01]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest_api as B
from positioning.anchor import HISTORY_PATH, load_history

CACHE_DIR = os.path.join("historical", "nifty1m")
NIFTY_KEY = "NSE_INDEX|Nifty 50"


# ----------------------------- data -----------------------------

def month_bounds(y: int, m: int) -> tuple[str, str]:
    first = date(y, m, 1)
    last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
    return first.isoformat(), last.isoformat()


def load_month(y: int, m: int) -> list[tuple[datetime, float, float, float, float]]:
    """[(ts_ist, o, h, l, c)] for one month, cached to CSV."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{y}-{m:02d}.csv")
    rows: list[tuple[datetime, float, float, float, float]] = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.reader(f):
                try:
                    rows.append((datetime.fromisoformat(r[0]),
                                 float(r[1]), float(r[2]), float(r[3]), float(r[4])))
                except (ValueError, IndexError):
                    continue
        return rows
    frm, to = month_bounds(y, m)
    try:
        raw = B.fetch_candles(NIFTY_KEY, frm, to)
    except Exception as exc:
        print(f"  [{y}-{m:02d}] fetch failed: {exc}")
        raw = []
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        for c in sorted(raw, key=lambda x: x[0]):
            ts = datetime.fromisoformat(c[0]).astimezone(B.IST)
            w.writerow([ts.isoformat(), c[1], c[2], c[3], c[4]])
            rows.append((ts, float(c[1]), float(c[2]), float(c[3]), float(c[4])))
    return rows


# ----------------------------- per-day metrics -----------------------------

def day_metrics(bars: list[tuple[datetime, float, float, float, float]]):
    """(ac5, trendiness) for one session's 1-min bars, or None if too thin."""
    if len(bars) < 200:
        return None
    closes = np.array([b[4] for b in bars])
    # 5-min resample on bar index (session bars are contiguous 1-min)
    c5 = closes[::5]
    r5 = np.diff(np.log(c5))
    if r5.size < 40 or np.std(r5) == 0:
        return None
    r = r5 - r5.mean()
    ac = float(np.dot(r[:-1], r[1:]) / np.dot(r, r))
    o = bars[0][1]
    hi = max(b[2] for b in bars)
    lo = min(b[3] for b in bars)
    c = bars[-1][4]
    trend = abs(c - o) / (hi - lo) if hi > lo else 0.0
    return ac, trend


# ----------------------------- study -----------------------------

def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean(); ry -= ry.mean()
    d = np.sqrt(np.dot(rx, rx) * np.dot(ry, ry))
    return float(np.dot(rx, ry) / d) if d > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_day", default="2023-08-01")
    args = ap.parse_args()
    from_day = date.fromisoformat(args.from_day)

    hist = load_history(HISTORY_PATH)
    score_dates = sorted(hist)
    if not score_dates:
        print("participant_history.csv is empty — run positioning/backfill.py first")
        return 1
    scores = {d: float(hist[d]["dealer_short_gamma_score"] or 0.0) for d in score_dates}

    def lagged_score(day_iso: str) -> float | None:
        import bisect
        i = bisect.bisect_left(score_dates, day_iso)
        return scores[score_dates[i - 1]] if i > 0 else None

    # walk months, group bars by session day
    today = date.today()
    obs: list[tuple[str, float, float, float]] = []   # (day, score_T-1, ac5, trend)
    y, m = from_day.year, from_day.month
    while (y, m) <= (today.year, today.month):
        bars = load_month(y, m)
        by_day: dict[str, list] = defaultdict(list)
        for b in bars:
            by_day[b[0].date().isoformat()].append(b)
        for d in sorted(by_day):
            if d < from_day.isoformat():
                continue
            s = lagged_score(d)
            if s is None:
                continue
            dm = day_metrics(sorted(by_day[d], key=lambda x: x[0]))
            if dm is None:
                continue
            obs.append((d, s, dm[0], dm[1]))
        m += 1
        if m > 12:
            m = 1; y += 1

    n = len(obs)
    if n < 100:
        print(f"only {n} joined observations — need more history before deciding")
        return 1
    days = [o[0] for o in obs]
    sc = np.array([o[1] for o in obs])
    ac = np.array([o[2] for o in obs])
    tr = np.array([o[3] for o in obs])

    print(f"A4 STUDY — {n} sessions joined ({days[0]} .. {days[-1]})")
    print(f"score distribution: p10={np.percentile(sc,10):+.4f}  median={np.median(sc):+.4f}  "
          f"p90={np.percentile(sc,90):+.4f}   (positive = dealers short gamma)")

    # tercile split
    lo_cut, hi_cut = np.percentile(sc, [33.33, 66.67])
    buckets = {
        "long-gamma (bottom)": sc < lo_cut,
        "mid": (sc >= lo_cut) & (sc < hi_cut),
        "short-gamma (top)": sc >= hi_cut,
    }
    print(f"\n{'tercile':<22} {'n':>5} {'mean AC(5m)':>12} {'mean trendiness':>16}")
    for name, mask in buckets.items():
        print(f"{name:<22} {mask.sum():>5} {ac[mask].mean():>+12.4f} {tr[mask].mean():>16.3f}")

    rho_ac = spearman(sc, ac)
    rho_tr = spearman(sc, tr)
    print(f"\nSpearman(score, AC5m)       = {rho_ac:+.3f}")
    print(f"Spearman(score, trendiness) = {rho_tr:+.3f}")

    # month-block bootstrap of top-minus-bottom AC difference
    months = np.array([d[:7] for d in days])
    uniq = np.unique(months)
    top_mask = buckets["short-gamma (top)"]; bot_mask = buckets["long-gamma (bottom)"]
    point = ac[top_mask].mean() - ac[bot_mask].mean()
    rng = np.random.default_rng(20260704)
    diffs = []
    for _ in range(2000):
        pick = rng.choice(uniq, size=uniq.size, replace=True)
        idx = np.concatenate([np.flatnonzero(months == mo) for mo in pick])
        t = idx[np.isin(idx, np.flatnonzero(top_mask))]
        b = idx[np.isin(idx, np.flatnonzero(bot_mask))]
        if t.size and b.size:
            diffs.append(ac[t].mean() - ac[b].mean())
    lo, hi = np.percentile(diffs, [5, 95])
    print(f"\nTOP-minus-BOTTOM tercile AC difference = {point:+.4f}   "
          f"90% block-bootstrap CI [{lo:+.4f}, {hi:+.4f}]")
    if point > 0 and lo > 0:
        print("VERDICT: GO — dealer-short-gamma days show more intraday momentum.")
    elif point < 0 and hi < 0:
        print("VERDICT: INVERTED — significant but opposite the hypothesis; "
              "sign convention needs rework before use.")
    else:
        print("VERDICT: NO-GO on this sample — CI straddles zero; anchor stays "
              "daily context, Phase C intraday evolution deprioritized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
