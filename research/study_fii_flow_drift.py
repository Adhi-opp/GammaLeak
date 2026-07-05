"""P9 study: does FII index-futures positioning (published evening T-1)
predict day-T NIFTY drift?

DATA SOURCES — deliberately none of the engine's own tick logs (they have
known gaps): participant positioning from data/participant_history.csv (NSE
archives, backfilled 2026-07-05) and prices from the Upstox historical API
1-min cache (historical/nifty1m/, exchange-official candles).

Pre-registered design (2026-07-05, before results were seen):

  Conditioning (both computed from files published BEFORE day T's open):
    PRIMARY    fii_flow  = fii_fut_net(T-1) - fii_fut_net(T-2)   (1-day flow)
    SECONDARY  fii_level = fii_fut_net(T-1) z-scored vs trailing 60 rows
  Outcomes for day T:
    PRIMARY    open->close log return (intraday drift — what the engine trades)
    SECONDARY  close(T-1)->open(T) gap, close->close return
  Tests:
    (a) top-minus-bottom flow-tercile mean O2C return, 90% month-block
        bootstrap CI; (b) sign-agreement hit rate P(sign(O2C)=sign(flow));
    (c) Spearman(flow, O2C); (d) momentum-confound control: partial Spearman
        given prior-day close->close return; (e) stability across halves.
  GO      : tercile spread CI excludes 0, sign stable across halves, survives
            the ret(T-1) partial.
  NO-GO   : CI straddles 0 (or effect dies under the partial) -> FII flow
            stays a dashboard context number, no prior wired.

Usage: python research/study_fii_flow_drift.py
"""
from __future__ import annotations

import os
import sys
import bisect
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from study_gamma_sign_autocorr import load_month, spearman
from positioning.anchor import HISTORY_PATH, load_history


def build_daily_frame() -> dict[str, tuple[float, float]]:
    """day-iso -> (open, close) from the exchange-official 1-min cache."""
    out: dict[str, tuple[float, float]] = {}
    y, m = 2023, 8
    while (y, m) <= (2026, 7):
        by = defaultdict(list)
        for b in load_month(y, m):
            by[b[0].date().isoformat()].append(b)
        for d, bars in by.items():
            bars.sort(key=lambda x: x[0])
            if len(bars) >= 300:                     # full-ish session only
                out[d] = (bars[0][1], bars[-1][4])   # first open, last close
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


def ranks(x: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(x)).astype(float)


def partial_spearman(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> float:
    """Spearman(x, y | z) via rank-regression residuals."""
    rx, ry, rz = ranks(x), ranks(y), ranks(z)
    bx = np.polyfit(rz, rx, 1); by_ = np.polyfit(rz, ry, 1)
    ex = rx - np.polyval(bx, rz); ey = ry - np.polyval(by_, rz)
    d = np.sqrt(np.dot(ex, ex) * np.dot(ey, ey))
    return float(np.dot(ex, ey) / d) if d > 0 else 0.0


def block_ci(metric_top: np.ndarray, metric_bot: np.ndarray,
             months_top: np.ndarray, months_bot: np.ndarray,
             n_boot: int = 2000, seed: int = 20260705) -> tuple[float, float, float]:
    point = metric_top.mean() - metric_bot.mean()
    uniq = np.unique(np.concatenate([months_top, months_bot]))
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        pick = rng.choice(uniq, size=uniq.size, replace=True)
        t = np.concatenate([metric_top[months_top == mo] for mo in pick])
        b = np.concatenate([metric_bot[months_bot == mo] for mo in pick])
        if t.size and b.size:
            diffs.append(t.mean() - b.mean())
    lo, hi = np.percentile(diffs, [5, 95])
    return point, float(lo), float(hi)


def main() -> int:
    hist = load_history(HISTORY_PATH)
    hd = sorted(hist)
    if len(hd) < 200:
        print("participant history too thin"); return 1
    fii_net = {}
    for d in hd:
        try:
            fii_net[d] = int(hist[d]["fii_fil"] or 0) - int(hist[d]["fii_fis"] or 0)
        except (KeyError, ValueError):
            continue
    nd = sorted(fii_net)

    daily = build_daily_frame()
    days = sorted(daily)

    obs = []   # (day, flow, level_z, o2c, gap, c2c, prev_c2c)
    for i, d in enumerate(days):
        if i < 2:
            continue
        # latest TWO published rows strictly before day d -> flow; 60 rows -> level z
        j = bisect.bisect_left(nd, d)
        if j < 61:
            continue
        d1, d2 = nd[j - 1], nd[j - 2]
        flow = fii_net[d1] - fii_net[d2]
        window = np.array([fii_net[x] for x in nd[j - 61:j - 1]], dtype=float)
        sd = window.std()
        level_z = (fii_net[d1] - window.mean()) / sd if sd > 0 else 0.0

        o, c = daily[d]
        po, pc = daily[days[i - 1]]
        ppo, ppc = daily[days[i - 2]]
        obs.append((d,
                    flow, level_z,
                    np.log(c / o),            # O2C drift (primary outcome)
                    np.log(o / pc),           # overnight gap
                    np.log(c / pc),           # close-to-close
                    np.log(pc / ppc)))        # prior-day C2C (confound control)

    n = len(obs)
    days_a = [o[0] for o in obs]
    months = np.array([d[:7] for d in days_a])
    flow = np.array([o[1] for o in obs], dtype=float)
    lvl = np.array([o[2] for o in obs])
    o2c = np.array([o[3] for o in obs]) * 1e4     # bps
    gap = np.array([o[4] for o in obs]) * 1e4
    c2c = np.array([o[5] for o in obs]) * 1e4
    pret = np.array([o[6] for o in obs]) * 1e4

    print(f"P9 STUDY — {n} sessions joined ({days_a[0]} .. {days_a[-1]})")
    print(f"fii flow (contracts/day): p10={np.percentile(flow,10):+,.0f}  "
          f"median={np.median(flow):+,.0f}  p90={np.percentile(flow,90):+,.0f}")

    lo_c, hi_c = np.percentile(flow, [33.33, 66.67])
    top = flow >= hi_c   # heavy FII buying
    bot = flow < lo_c    # heavy FII selling

    print(f"\n{'flow tercile':<18} {'n':>5} {'mean O2C bps':>13} {'mean gap bps':>13} {'mean C2C bps':>13}")
    for name, mask in (("selling (bottom)", bot), ("mid", ~top & ~bot), ("buying (top)", top)):
        print(f"{name:<18} {mask.sum():>5} {o2c[mask].mean():>+13.1f} "
              f"{gap[mask].mean():>+13.1f} {c2c[mask].mean():>+13.1f}")

    p, lo, hi = block_ci(o2c[top], o2c[bot], months[top], months[bot])
    print(f"\nPRIMARY  top-minus-bottom O2C: {p:+.1f} bps   90% CI [{lo:+.1f}, {hi:+.1f}]")
    pg, log_, hig = block_ci(gap[top], gap[bot], months[top], months[bot])
    print(f"         top-minus-bottom gap: {pg:+.1f} bps   90% CI [{log_:+.1f}, {hig:+.1f}]")

    nz = flow != 0
    hit = (np.sign(o2c[nz]) == np.sign(flow[nz])).mean() * 100
    print(f"sign-agreement (flow -> O2C): {hit:.1f}%  (n={nz.sum()})")
    print(f"Spearman(flow, O2C)                 = {spearman(flow, o2c):+.3f}")
    print(f"Spearman(flow, O2C | prior-day ret) = {partial_spearman(flow, o2c, pret):+.3f}")
    print(f"Spearman(level_z, O2C)              = {spearman(lvl, o2c):+.3f}")

    half = n // 2
    for name, sl in (("first half", slice(0, half)), ("second half", slice(half, None))):
        print(f"  {name}: Spearman(flow, O2C) = {spearman(flow[sl], o2c[sl]):+.3f}  (n={len(o2c[sl])})")

    go = p > 0 and lo > 0
    inv = p < 0 and hi < 0
    if go:
        print("\nVERDICT: GO — FII flow continues into next-day intraday drift.")
    elif inv:
        print("\nVERDICT: INVERTED — significant contrarian effect; re-derive sign before use.")
    else:
        print("\nVERDICT: NO-GO on this sample — CI straddles zero; FII flow stays "
              "context-only, no prior wired.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
