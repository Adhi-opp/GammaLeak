"""
API-based backtester — runs the full engine signal path on fresh 1-min candles
fetched from the Upstox historical API (or cached CSVs in historical/).

Grades every CONFIRM signal by 10-min forward MFE and prints a breakdown
by: feature/setup, conviction score, regime, and session day.

Grading convention (fixed 2026-07-03): entry = the CONFIRM bar close (the
tradeable fill), forward window clamped to the same session, MAE reported
alongside MFE. The old convention graded from the ALERT price, re-counting
the move that triggered confirmation — it inflated 22% to 79% on identical
fires. Numbers produced before this fix are not comparable.

Usage:
    python backtest_api.py                          # all cached historical CSVs
    python backtest_api.py --fetch                  # re-fetch May candles from API
    python backtest_api.py --fetch --from 2026-04-01 --to 2026-05-29
    python backtest_api.py --symbol NIFTY           # single symbol
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests
from dotenv import load_dotenv

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
BASE_URL = "https://api.upstox.com/v2/historical-candle"
HIST_DIR = "historical"

SYMBOLS = {
    "NIFTY":        "NSE_INDEX|Nifty 50",
    "BANKNIFTY":    "NSE_INDEX|Nifty Bank",
    "SENSEX":       "BSE_INDEX|SENSEX",
    "NIFTY_FUT":    "NSE_FO|62329",     # NIFTY26JUNFUT — covers May period
    "BANKNIFTY_FUT":"NSE_FO|62326",     # BANKNIFTY26JUNFUT
    "SENSEX_FUT":   "BSE_FO|1105863",   # SENSEX26JUNFUT
}

FWD_WINDOW_SECS = 600        # 10-min forward MFE window
MFE_ATR_K      = 0.5         # ATR-scaled bar
MFE_FLOORS     = {           # per-symbol noise floor (points)
    "NIFTY": 15.0,     "NIFTY_FUT": 15.0,
    "BANKNIFTY": 40.0, "BANKNIFTY_FUT": 40.0,
    "SENSEX": 50.0,    "SENSEX_FUT": 50.0,
}

# Rolling windows for the engine (seconds of 1-min candles = N bars of 60s each)
WARMUP_BARS   = 20           # bars before any signal can fire
VWAP_WIN_BARS = 390          # ~full day
SD_WIN_BARS   = 60           # 60-min rolling σ
ER_WIN_BARS   = 70           # ~70 bars Kaufman ER lookback
HURST_WIN     = 128          # R/S hurst window (bars)
MICRO_Z_BARS  = 15           # 15-bar rolling micro-Z

SIGNAL_ALERT_Z    = 3.0
SIGNAL_CONFIRM_Z  = 2.5
SIGNAL_EXIT_Z     = 1.0
EXHAUSTION_PEAK   = 4.0
ER_TREND_THRESH   = 0.60
HURST_THRESH      = 0.55

ATR_PERIOD = 14              # 1-min ATR for bars
ATR_PROXY_BARS = 5           # 5-bar range as floor for MFE threshold

SESSION_OPEN  = (9, 15)
WARMUP_END    = (9, 20)


# ─────────────────────────── DATA FETCH ───────────────────────────

def fetch_candles(instrument_key: str, from_date: str, to_date: str) -> list:
    from urllib.parse import quote
    url = f"{BASE_URL}/{quote(instrument_key, safe='')}/1minute/{to_date}/{from_date}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    for attempt in range(3):
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json().get("data", {}).get("candles", [])
    return []


def load_or_fetch(symbol: str, from_d: str, to_d: str) -> list[tuple[datetime, float, float, float, float, int]]:
    """Return sorted list of (ts, open, high, low, close, volume) 1-min bars."""
    # Try existing CSV files in historical/ first
    pattern = os.path.join(HIST_DIR, f"{symbol}_1minute_*.csv")
    candidates = sorted(glob.glob(pattern))
    rows: list[tuple] = []
    for path in candidates:
        with open(path, newline="") as f:
            for row in csv.reader(f):
                if row[0] == "timestamp": continue
                try:
                    ts = datetime.fromisoformat(row[0])
                    if ts.tzinfo is None: ts = ts.replace(tzinfo=IST)
                    rows.append((ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), int(float(row[5]))))
                except (ValueError, IndexError):
                    continue
    if not rows:
        print(f"  [{symbol}] no cached data found, fetching from API…")
        key = SYMBOLS.get(symbol)
        if not key: return []
        raw = fetch_candles(key, from_d, to_d)
        os.makedirs(HIST_DIR, exist_ok=True)
        fname = os.path.join(HIST_DIR, f"{symbol}_1minute_{from_d}_to_{to_d}.csv")
        with open(fname, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp","open","high","low","close","volume","oi"])
            for c in sorted(raw, key=lambda x: x[0]):
                w.writerow(c)
        for c in raw:
            ts = datetime.fromisoformat(c[0])
            if ts.tzinfo is None: ts = ts.replace(tzinfo=IST)
            rows.append((ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), int(float(c[5]))))

    # Filter to requested range
    from_dt = datetime.fromisoformat(from_d).replace(tzinfo=IST)
    to_dt   = (datetime.fromisoformat(to_d) + timedelta(days=1)).replace(tzinfo=IST)
    rows = [r for r in rows if from_dt <= r[0] < to_dt]
    rows.sort(key=lambda x: x[0])
    return rows


# ─────────────────────────── ENGINE MATHS ───────────────────────────

def compute_er(closes: np.ndarray) -> float:
    if closes.size < 2: return 0.0
    net = abs(float(closes[-1] - closes[0]))
    path = float(np.abs(np.diff(closes)).sum())
    return net / path if path > 0 else 0.0


def compute_hurst(prices: np.ndarray) -> float:
    if prices.size < 20: return 0.5
    inc = np.diff(prices)
    windows = np.array([8, 16, 32, 64, 128], dtype=np.int64)
    windows = windows[windows <= inc.size]
    if windows.size < 2: return 0.5
    pts = []
    for w in windows:
        n = inc.size // int(w)
        if n < 2: continue
        seg = inc[-n*int(w):].reshape(n, int(w))
        dm = seg - seg.mean(axis=1, keepdims=True)
        cum = np.cumsum(dm, axis=1)
        r = cum.max(axis=1) - cum.min(axis=1)
        s = seg.std(axis=1)
        v = s > 0
        if not np.any(v): continue
        rs = r[v] / s[v]; rs = rs[rs > 0]
        if rs.size == 0: continue
        pts.append((float(w), float(rs.mean())))
    if len(pts) < 2: return 0.5
    lw = np.log(np.array([p[0] for p in pts]))
    lr = np.log(np.array([p[1] for p in pts]))
    return float(np.clip(np.polyfit(lw, lr, 1)[0], 0, 1))


def compute_atr(closes: np.ndarray, n: int = ATR_PERIOD) -> float:
    if closes.size < n + 1: return 0.0
    trs = np.abs(np.diff(closes[-n-1:]))   # simplified TR for index (no gap at 1-min)
    return float(trs.mean())


def mfe_floor(symbol: str, closes: np.ndarray, entry: float) -> float:
    floor = MFE_FLOORS.get(symbol, 20.0)
    if closes.size >= ATR_PROXY_BARS + 1:
        atr = float(np.max(closes[-ATR_PROXY_BARS:]) - np.min(closes[-ATR_PROXY_BARS:]))
        return max(MFE_ATR_K * atr, floor)
    return floor


# ─────────────────────────── SIGNAL SIMULATION ───────────────────────────

def simulate(symbol: str, bars: list[tuple]) -> list[dict]:
    """Run the Z-score state machine + grading on a list of 1-min bars.
    Returns a list of graded CONFIRM fires.
    """
    closes  = []
    cum_vol = 0
    cum_pv  = 0.0
    cum_cnt = 0       # bar count fallback when volume=0 (index instruments)

    sig_state  = 0
    alert_side = 0
    peak_z     = 0.0
    entry_ltp  = 0.0
    mfe_live   = 0.0
    prev_z     = 0.0

    fires: list[dict] = []

    prev_day: date | None = None

    for i, (ts, o, h, l, c, vol) in enumerate(bars):
        ist = ts.astimezone(IST)

        # Session rollover
        day = ist.date()
        if day != prev_day:
            closes = []
            cum_vol = 0; cum_pv = 0.0; cum_cnt = 0
            sig_state = 0; alert_side = 0; peak_z = 0.0
            entry_ltp = 0.0; mfe_live = 0.0; prev_z = 0.0
            prev_day = day

        closes.append(c)
        cum_cnt += 1
        if vol > 0:
            cum_vol += vol
            cum_pv  += c * vol
        arr = np.asarray(closes, dtype=np.float64)

        # Warmup
        if (ist.hour, ist.minute) < WARMUP_END:
            prev_z = 0.0
            continue
        if len(closes) < WARMUP_BARS:
            prev_z = 0.0
            continue

        # VWAP — use volume-weighted when vol exists, equal-weight mean for
        # index instruments (historical candles have volume=0 for NSE_INDEX).
        if cum_vol > 0:
            vwap = cum_pv / cum_vol
        else:
            vwap = float(np.mean(arr))   # equal-weight: same Z-score semantics
        window = arr[-SD_WIN_BARS:]
        sd     = float(np.std(window)) if window.size >= 10 else 0.0
        sd     = max(sd, 0.01)
        z      = float(np.clip((c - vwap) / sd, -10, 10))

        # ER / Hurst
        er_arr = arr[-ER_WIN_BARS:] if len(arr) >= ER_WIN_BARS else arr
        er     = compute_er(er_arr)
        hu     = compute_hurst(arr[-HURST_WIN:]) if len(arr) >= 20 else 0.5

        # Regime: trending = suppress fade
        trending = (er >= ER_TREND_THRESH) or (hu >= HURST_THRESH)
        side     = 1 if z > 0 else (-1 if z < 0 else 0)
        abs_z    = abs(z)

        # Running MFE
        if sig_state >= 1 and alert_side != 0:
            fav = (entry_ltp - c) if alert_side > 0 else (c - entry_ltp)
            mfe_live = max(mfe_live, fav)

        # --- State machine ---
        if sig_state == 0:
            if not trending and side != 0 and abs_z >= SIGNAL_ALERT_Z:
                sig_state  = 1
                alert_side = side
                peak_z     = z
                entry_ltp  = c
                mfe_live   = 0.0

        elif sig_state == 1:
            if side == alert_side and abs_z >= SIGNAL_ALERT_Z:
                if abs_z > abs(peak_z): peak_z = z
            elif abs(prev_z) > SIGNAL_CONFIRM_Z and abs_z <= SIGNAL_CONFIRM_Z:
                # Z reversed — check MFE gate
                fl = mfe_floor(symbol, arr, entry_ltp)
                if mfe_live >= fl:
                    # CONFIRM — compute conviction factors
                    is_exh   = abs(peak_z) >= EXHAUSTION_PEAK
                    is_chop  = er < ER_TREND_THRESH and hu < HURST_THRESH
                    micro_w  = arr[-MICRO_Z_BARS:] if len(arr) >= MICRO_Z_BARS else arr
                    micro_m  = float(np.mean(micro_w)); micro_sd = float(np.std(micro_w))
                    micro_z  = (c - micro_m) / max(micro_sd, 0.01)
                    div_tags = []   # no CVD in 1-min bars — will always be empty
                    factors  = []
                    if is_exh:  factors.append("EXH")
                    if is_chop: factors.append("CHOP")
                    # Weight-based conviction
                    from core.config import CONVICTION_FACTOR_WEIGHTS as CW
                    conv = 1 + sum(CW.get(f, 0) for f in factors)
                    conv = min(5, max(1, conv))

                    # Grade: 10-min forward MFE/MAE from the CONFIRM close —
                    # the tradeable fill — with the window clamped to the same
                    # session (never across the overnight gap). The previous
                    # convention graded from the ALERT price, which re-counted
                    # the very move that triggered confirmation (audited
                    # 2026-07-03: 79% -> 22% on identical fires). This matches
                    # calibrate.py's live convention (entry = CONFIRM ltp).
                    future = []
                    for j in range(i + 1, min(i + 11, len(bars))):
                        if bars[j][0].astimezone(IST).date() != day:
                            break
                        future.append(bars[j][4])
                    if future:
                        fut = np.asarray(future)
                        if alert_side > 0:      # fade short
                            mfe_fwd = float(c - np.min(fut))
                            mae_fwd = float(np.max(fut) - c)
                        else:                   # fade long
                            mfe_fwd = float(np.max(fut) - c)
                            mae_fwd = float(c - np.min(fut))
                        mfe_fwd = max(0.0, mfe_fwd)
                        mae_fwd = max(0.0, mae_fwd)
                        threshold = mfe_floor(symbol, arr, c)
                        win = mfe_fwd >= threshold
                        setup = "EXHAUSTION REV" if is_exh else "FADE LOW"
                        setup += " S" if alert_side > 0 else " L"
                        fires.append({
                            "day":     day.isoformat(),
                            "time":    ist.strftime("%H:%M"),
                            "symbol":  symbol,
                            "setup":   setup,
                            "conv":    conv,
                            "factors": ",".join(factors) or "—",
                            "side":    alert_side,
                            "peak_z":  round(peak_z, 2),
                            "er":      round(er, 3),
                            "hurst":   round(hu, 3),
                            "micro_z": round(micro_z, 2),
                            "entry":   round(c, 2),           # CONFIRM close (graded fill)
                            "alert_entry": round(entry_ltp, 2),
                            "mfe_fwd": round(mfe_fwd, 2),
                            "mae_fwd": round(mae_fwd, 2),
                            "floor":   round(threshold, 2),
                            "win":     win,
                        })
                    sig_state = 0; alert_side = 0; peak_z = 0.0
                else:
                    pass  # MFE_PENDING, stay in state 1
            elif side != 0 and side != alert_side and abs_z >= SIGNAL_ALERT_Z:
                alert_side = side; peak_z = z; entry_ltp = c; mfe_live = 0.0
            elif abs_z < SIGNAL_EXIT_Z:
                sig_state = 0; alert_side = 0; peak_z = 0.0

        prev_z = z

    return fires


# ─────────────────────────── REPORT ───────────────────────────

def report(all_fires: list[dict]):
    N = len(all_fires)
    if N == 0:
        print("No confirmed signals found.")
        return
    wins = sum(1 for f in all_fires if f["win"])
    base = wins / N * 100
    avg_mfe = np.mean([f["mfe_fwd"] for f in all_fires])
    avg_mae = np.mean([f["mae_fwd"] for f in all_fires])
    print(f"\n{'='*65}")
    print(f"BACKTEST SUMMARY  —  {N} CONFIRMs  |  base hit {base:.0f}%  ({wins}/{N})")
    print(f"  (10-min fwd MFE from CONFIRM close — tradeable fill, same-session")
    print(f"   window, vs ATR-scaled floor. avg MFE {avg_mfe:.1f} / avg MAE {avg_mae:.1f} pts;")
    print(f"   MFE-only 'win' is NOT pnl — check the MFE/MAE ratio before believing it)")
    print('='*65)

    # By symbol
    print("\nBY SYMBOL:")
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for f in all_fires: by_sym[f["symbol"]].append(f)
    for sym in sorted(by_sym):
        fs = by_sym[sym]; n = len(fs); w = sum(1 for f in fs if f["win"])
        print(f"  {sym:<12} {w:3d}/{n:3d} = {w/n*100:4.0f}%")

    # By setup
    print("\nBY SETUP:")
    by_setup: dict[str, list[dict]] = defaultdict(list)
    for f in all_fires: by_setup[f["setup"]].append(f)
    for setup in sorted(by_setup, key=lambda s: -len(by_setup[s])):
        fs = by_setup[setup]; n = len(fs); w = sum(1 for f in fs if f["win"])
        print(f"  {setup:<22} {w:3d}/{n:3d} = {w/n*100:4.0f}%")

    # By conviction score (with the NEW weighted model)
    print("\nBY CONVICTION SCORE  (weighted model: EXH=2, DIV=1, CHOP=1):")
    by_conv: dict[int, list[dict]] = defaultdict(list)
    for f in all_fires: by_conv[f["conv"]].append(f)
    for cv in sorted(by_conv):
        fs = by_conv[cv]; n = len(fs); w = sum(1 for f in fs if f["win"])
        print(f"  conv={cv}   {w:3d}/{n:3d} = {w/n*100:4.0f}%")

    # Per-factor lift
    print("\nFACTOR LIFT  (present vs absent):")
    for fac in ("EXH", "CHOP"):
        p = [f["win"] for f in all_fires if fac in f["factors"].split(",")]
        a = [f["win"] for f in all_fires if fac not in f["factors"].split(",")]
        if not p or not a:
            print(f"  {fac:<6}  (no data on one side)")
            continue
        hp = sum(p)/len(p)*100; ha = sum(a)/len(a)*100
        print(f"  {fac:<6}  present {sum(p)}/{len(p)}={hp:.0f}%   absent {sum(a)}/{len(a)}={ha:.0f}%   lift {hp-ha:+.0f}")

    # EXH gate vs non-gate
    exh  = [f for f in all_fires if "EXH"  in f["factors"]]
    nexp = [f for f in all_fires if "EXH" not in f["factors"]]
    if exh and nexp:
        print(f"\n  EXHAUSTION setups:      {sum(f['win'] for f in exh)}/{len(exh)} "
              f"= {sum(f['win'] for f in exh)/len(exh)*100:.0f}%")
        print(f"  Non-exhaustion setups:  {sum(f['win'] for f in nexp)}/{len(nexp)} "
              f"= {sum(f['win'] for f in nexp)/len(nexp)*100:.0f}%")

    # Regime gate effectiveness (ORB vs non-ORB)
    print("\nBY SESSION (day-level hit rate):")
    by_day: dict[str, list[dict]] = defaultdict(list)
    for f in all_fires: by_day[f["day"]].append(f)
    for d in sorted(by_day):
        fs = by_day[d]; n = len(fs); w = sum(1 for f in fs if f["win"])
        print(f"  {d}   {w:2d}/{n:2d} = {w/n*100:3.0f}%")

    print()


# ─────────────────────────── MAIN ───────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="NIFTY / BANKNIFTY / SENSEX or ALL")
    ap.add_argument("--from",   dest="from_date", default="2026-05-01")
    ap.add_argument("--to",     dest="to_date",   default="2026-05-29")
    ap.add_argument("--fetch",  action="store_true", help="Re-fetch from API")
    args = ap.parse_args()

    syms = [args.symbol.upper()] if args.symbol else list(SYMBOLS.keys())

    if args.fetch:
        if not ACCESS_TOKEN:
            print("ERROR: UPSTOX_ACCESS_TOKEN not set in .env"); return
        for sym in syms:
            key = SYMBOLS.get(sym)
            if not key: continue
            print(f"Fetching {sym} {args.from_date}→{args.to_date}…")
            raw = fetch_candles(key, args.from_date, args.to_date)
            fname = os.path.join(HIST_DIR, f"{sym}_1minute_{args.from_date}_to_{args.to_date}.csv")
            os.makedirs(HIST_DIR, exist_ok=True)
            with open(fname, "w", newline="") as f:
                w = csv.writer(f); w.writerow(["timestamp","open","high","low","close","volume","oi"])
                for c in sorted(raw, key=lambda x: x[0]): w.writerow(c)
            print(f"  saved {len(raw)} bars -> {fname}")

    all_fires: list[dict] = []
    for sym in syms:
        bars = load_or_fetch(sym, args.from_date, args.to_date)
        if not bars:
            print(f"  [{sym}] no data — skip")
            continue
        print(f"  [{sym}] {len(bars)} bars loaded ({bars[0][0].date()} to {bars[-1][0].date()})")
        fires = simulate(sym, bars)
        all_fires.extend(fires)
        w = sum(1 for f in fires if f["win"])
        print(f"         {len(fires)} confirms, {w}/{len(fires)} = {w/len(fires)*100:.0f}% hit" if fires else "         0 confirms")

    report(all_fires)


if __name__ == "__main__":
    main()
