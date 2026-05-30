"""Nightly calibration grader — turns the frozen, hand-tuned gate into a living
one re-derived from realised outcomes.

What it does
------------
1. Reads every `logs/<date>_events.csv` ground-truth fire log.
2. Grades each CONFIRM by Maximum Favorable Excursion (MFE) over a forward
   window, using the per-tick price series in `logs/<date>/<SYMBOL>.csv`.
3. Pools the wins/losses into a `(setup_label x regime)` table (gate is
   symbol-agnostic) and a per-symbol MFE-magnitude table.
4. Counterfactually grades the REGIME_BLOCK aborts — "what would the blocked
   fires have done?" — so the gate can be validated and relaxed if it's wrong.
5. Emits `core/calibration.py` (pure literals, committed) with the re-derived
   `CALIBRATED_REGIME_RULES` + advisory `CALIBRATED_MFE_FLOORS`, plus a
   provenance header (date, sessions, sample sizes). `core/config.py` merges
   these over the hand defaults at import time.

This is statistical *calibration*, not ML. With min-sample guards a thin
table just leaves a setup untouched rather than inventing a rule from 3 fires.
As sessions accumulate the table sharpens on its own — that's the whole point.

Usage:
    python calibrate.py                 # all sessions
    python calibrate.py --last 20       # only the most recent 20 sessions
    python calibrate.py --dry-run       # print table, don't write calibration.py
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
from collections import defaultdict
from datetime import datetime

import numpy as np

from core.config import (
    MIN_FAVORABLE_POINTS_PER_SYMBOL,
    MFE_ATR_K,
    REVIEW_DEFAULT_MIN_FAVORABLE_PCT,
    ER_TREND_THRESHOLD,
    HURST_THRESHOLD,
    HAND_CONVICTION_WEIGHTS,
)

# ----------------------------- KNOBS -----------------------------
FWD_WINDOW_SECS = 600           # 10-min forward MFE window (engine design intent)
ATR_PROXY_WINDOW_SECS = 300     # prior-5-min realised range = ATR proxy
MIN_FWD_POINTS = 5              # need at least this many forward ticks to grade
MIN_FWD_SPAN_SECS = 120         # ...and at least this much forward time

MIN_SAMPLE_GATE = 10            # a (setup,regime) pair needs n>=this for a rule
BLOCK_BELOW = 0.25              # pooled hit-rate < this  -> BLOCK
REQUIRE_CONV_BELOW = 0.40       # hit < this = underperforming (try to rescue/gate)
MIN_CONV_SUBSET = 5             # conv>=t subset needs n>=this to prescribe it
CONV_LEVELS_TRIED = (4, 5)      # conviction bars tested, lowest that rescues wins

MIN_SAMPLE_FLOOR = 10           # per-symbol fires needed to re-derive a floor
FLOOR_PCTILE = 33               # floor sits at this pctile of observed MFE magnitudes
FLOOR_CLAMP = (0.5, 2.0)        # calibrated floor stays within [0.5x, 2x] of hand floor

# Conviction factor lift -> weight. A factor needs >= MIN_FACTOR_SAMPLE fires on
# BOTH sides (present and absent) before its weight is re-derived; otherwise the
# hand weight stands. Lift = hit%(present) - hit%(absent), graded vs the fixed
# MFE floor (non-circular).
ALL_FACTORS = ("EXH", "CHOP", "DIV", "OIF", "DRIFT", "OFI")
MIN_FACTOR_SAMPLE = 15
LIFT_W2 = 15.0                  # lift (pct points) >= this -> weight 2
LIFT_W1 = 7.0                   # lift >= this -> weight 1; below (incl negative) -> 0

LOG_DIR = "logs"
OUT_PATH = os.path.join("core", "calibration.py")


# ----------------------------- LOADING -----------------------------

def list_event_files(last_n: int | None) -> list[str]:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "*_events.csv")))
    if last_n is not None and last_n > 0:
        files = files[-last_n:]
    return files


_price_cache: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}


def load_price_series(date_str: str, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (timestamps, prices) for one symbol on one date. Cached."""
    key = (date_str, symbol)
    if key in _price_cache:
        return _price_cache[key]
    path = os.path.join(LOG_DIR, date_str, f"{symbol}.csv")
    ts_list: list[float] = []
    px_list: list[float] = []
    try:
        with open(path, newline="") as f:
            r = csv.reader(f)
            next(r, None)  # header
            for row in r:
                if len(row) < 3:
                    continue
                try:
                    ts_list.append(float(row[0]))
                    px_list.append(float(row[2]))  # ltp column
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    ts = np.asarray(ts_list, dtype=np.float64)
    px = np.asarray(px_list, dtype=np.float64)
    _price_cache[key] = (ts, px)
    return ts, px


_feat_cache: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, np.ndarray, list]] = {}


def load_features(date_str: str, symbol: str):
    """Return (ts, er, hurst, divergence[]) for one symbol/date from the per-tick
    CSV — used to reconstruct conviction factors at fire time for sessions logged
    before conv_factors instrumentation existed. Cached."""
    key = (date_str, symbol)
    if key in _feat_cache:
        return _feat_cache[key]
    path = os.path.join(LOG_DIR, date_str, f"{symbol}.csv")
    ts: list[float] = []
    er: list[float] = []
    hu: list[float] = []
    dv: list[str] = []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts.append(float(row["timestamp"]))
                    er.append(float(row["er"]))
                    hu.append(float(row["hurst"]))
                    dv.append(row.get("divergence", "") or "")
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    out = (np.asarray(ts), np.asarray(er), np.asarray(hu), dv)
    _feat_cache[key] = out
    return out


def reconstruct_factors(date_str: str, symbol: str, side: int, setup: str, fire_ts: float) -> dict:
    """Best-effort factor truth at fire time for a pre-instrumentation CONFIRM.

    Only EXH / CHOP / DIV are recoverable from the per-tick CSV; OIF / DRIFT / OFI
    weren't logged, so they're left None (unknown) and excluded from their lift
    tally rather than miscounted as absent.
    """
    f: dict[str, bool | None] = {k: None for k in ALL_FACTORS}
    f["EXH"] = setup.startswith("EXHAUSTION")
    ts, er, hu, dv = load_features(date_str, symbol)
    if ts.size:
        j = int(np.searchsorted(ts, fire_ts, side="right")) - 1
        if j >= 0:
            f["CHOP"] = bool(er[j] < ER_TREND_THRESHOLD and hu[j] < HURST_THRESHOLD)
            d = dv[j]
            f["DIV"] = bool(
                (side > 0 and d in ("BUYER_EXHAUSTION", "BUY_ABSORPTION"))
                or (side < 0 and d in ("SELLER_EXHAUSTION", "SELL_ABSORPTION"))
            )
    return f


# ----------------------------- GRADING -----------------------------

def _per_symbol_floor(symbol: str, entry: float) -> float:
    table = MIN_FAVORABLE_POINTS_PER_SYMBOL.get(symbol)
    if table is not None:
        return float(table)
    return abs(entry) * (REVIEW_DEFAULT_MIN_FAVORABLE_PCT / 100.0)


def grade_fire(
    date_str: str, symbol: str, side: int, entry: float, fire_ts: float,
) -> tuple[float, float, bool] | None:
    """Grade one fire. side = stretch side (+1 stretched high -> fade short).

    Returns (mfe_points, floor, win) or None if ungradable (insufficient
    forward data / unknown symbol series).
    """
    ts, px = load_price_series(date_str, symbol)
    if ts.size == 0 or side == 0:
        return None

    j = int(np.searchsorted(ts, fire_ts, side="left"))
    k = int(np.searchsorted(ts, fire_ts + FWD_WINDOW_SECS, side="right"))
    fwd = px[j:k]
    if fwd.size < MIN_FWD_POINTS:
        return None
    if float(ts[min(k, ts.size) - 1]) - fire_ts < MIN_FWD_SPAN_SECS:
        return None

    # MFE in the fade direction (engine convention: side>0 stretched high, the
    # trade is short, so favorable = entry - min).
    if side > 0:
        mfe = entry - float(np.min(fwd))
    else:
        mfe = float(np.max(fwd)) - entry
    mfe = max(0.0, mfe)

    # ATR proxy = prior-5-min realised range; bar = max(K*atr, per-symbol floor).
    start = int(np.searchsorted(ts, fire_ts - ATR_PROXY_WINDOW_SECS, side="left"))
    start = max(0, min(start, j))
    atr_proxy = float(np.max(px[start:j + 1]) - np.min(px[start:j + 1])) if j > start else 0.0
    floor = max(MFE_ATR_K * atr_proxy, _per_symbol_floor(symbol, entry))

    return mfe, floor, (mfe >= floor)


# ----------------------------- REGIME BUCKETING -----------------------------

# Priority order: the more "notable" condition wins when a regime string carries
# several tags (e.g. "THE PIN | BN DIVERGE" -> DIVERGE). The emitted substring is
# what the runtime gate matches against state.regime via `substr in regime`.
_REGIME_BUCKETS = ("DIVERGE", "GAMMA SQUEEZE", "EXPANSION", "THE PIN")


def bucket_regime(regime: str) -> str | None:
    if not regime or "BLACKOUT" in regime:
        return None
    for b in _REGIME_BUCKETS:
        if b in regime:
            return b
    return "NORMAL"


# ----------------------------- AGGREGATION -----------------------------

def collect(files: list[str]):
    """Build the UNcensored population per (setup, regime).

    The live gate censors its own data — a blocked fire never becomes a CONFIRM,
    so grading CONFIRMs alone hides why the gate exists. We pool both:
      - CONFIRMs                  -> (conv, win, blocked=False)
      - counterfactual BLOCK aborts -> (conv=-1, win, blocked=True)
    so the base rate is the true population and the gate can be re-derived (or
    relaxed) against unbiased data. The conviction-rescue test uses only the
    confirmed subset (blocked rows record conviction=0, not their real score).
    """
    # (setup, regime) -> list of (conv | -1, win, blocked)
    population: dict[tuple[str, str], list[tuple[int, int, bool]]] = defaultdict(list)
    sym_mfe: dict[str, list[float]] = defaultdict(list)   # per-symbol MFE magnitudes
    # factor -> {True: [wins...], False: [wins...]} for present/absent lift
    factor_lift: dict[str, dict[bool, list[int]]] = defaultdict(lambda: {True: [], False: []})
    ungraded = 0
    total_confirms = 0

    for fpath in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})_events\.csv$", os.path.basename(fpath))
        if not m:
            continue
        date_str = m.group(1)
        with open(fpath, newline="") as f:
            for row in csv.DictReader(f):
                etype = row.get("event_type", "")
                symbol = row.get("symbol", "")
                setup = (row.get("setup_label", "") or "").strip()
                regime = row.get("regime", "") or ""
                try:
                    side = int(float(row.get("side", "0") or 0))
                    entry = float(row.get("ltp", "0") or 0)
                    fire_ts = float(row.get("timestamp", "0") or 0)
                    conv = int(float(row.get("conviction", "0") or 0))
                except ValueError:
                    continue

                if etype == "CONFIRM" and setup and not setup.startswith("REGIME_BLOCK"):
                    total_confirms += 1
                    rb = bucket_regime(regime)
                    if rb is None:
                        continue
                    g = grade_fire(date_str, symbol, side, entry, fire_ts)
                    if g is None:
                        ungraded += 1
                        continue
                    mfe, _floor, win = g
                    population[(setup, rb)].append((conv, int(win), False))
                    sym_mfe[symbol].append(mfe)

                    # Per-factor lift: use the logged conv_factors when the column
                    # exists (None == pre-instrumentation session -> reconstruct).
                    cf = row.get("conv_factors")
                    if cf is not None:
                        tags = {t for t in cf.split(",") if t}
                        ftruth = {k: (k in tags) for k in ALL_FACTORS}
                    else:
                        ftruth = reconstruct_factors(date_str, symbol, side, setup, fire_ts)
                    for fac, present in ftruth.items():
                        if present is None:
                            continue
                        factor_lift[fac][present].append(int(win))

                elif etype == "ABORT" and setup.startswith("REGIME_BLOCK:"):
                    # setup_label = "REGIME_BLOCK:<orig setup>|<regime>[|CONV<n>]"
                    payload = setup.split(":", 1)[1]
                    orig_setup = payload.split("|", 1)[0].strip()
                    rb = bucket_regime(regime)
                    if rb is None:
                        continue
                    g = grade_fire(date_str, symbol, side, entry, fire_ts)
                    if g is None:
                        continue
                    _mfe, _floor, win = g
                    population[(orig_setup, rb)].append((-1, int(win), True))

    return population, sym_mfe, factor_lift, ungraded, total_confirms


# ----------------------------- RULE / FLOOR DERIVATION -----------------------------

def derive_rules(population: dict[tuple[str, str], list[tuple[int, int, bool]]]):
    """Return [(setup, regime, action, n, wins)] for underperforming pairs.

    Returns (rules, relax). `relax` lists clean-recovery pairs authorised to drop
    a hand rule. Base rate uses the UNcensored population (confirms + blocked
    counterfactuals). Decision tree per (setup, regime) with n >= MIN_SAMPLE_GATE:
      hit >= 40%                                       -> PASS  (-> relax list)
      else, if conv>=t CONFIRM subset (n>=5) hits >=40% -> REQUIRE_CONV_t (lowest)
      else, if hit < 25%                                -> BLOCK
      else (25-40%, conviction can't rescue)            -> WATCH (no rule emitted)

    The conviction-rescue test only fires when raising the bar genuinely
    separates winners — so REQUIRE_CONV is never a silent total block, and a
    setup where conviction is *inversely* predictive (e.g. ORB BREAK S) is not
    "fixed" by demanding more of it. A 25-40% setup that conviction can't rescue
    is left as WATCH rather than blocked: not confident enough on this sample.
    """
    rules = []
    relax: list[tuple[str, str]] = []   # clean PASS pairs — authorised to drop a hand rule
    for (setup, regime), fires in sorted(population.items()):
        n = len(fires)
        if n < MIN_SAMPLE_GATE:
            continue                        # thin: hand rule (if any) stays untouched
        wins = sum(w for _c, w, _b in fires)
        hit = wins / n
        if hit >= REQUIRE_CONV_BELOW:
            # Clean recovery: relax conservatively (only on a real >=40% pass, so a
            # human's hand block is never undone on a coin-flip-band read).
            relax.append((setup, regime))
            continue

        # rescue test on the confirmed subset only (blocked rows lack real conv)
        confirmed = [(c, w) for c, w, b in fires if not b and c >= 0]
        rescue = None
        for t in CONV_LEVELS_TRIED:
            sub = [w for c, w in confirmed if c >= t]
            if len(sub) >= MIN_CONV_SUBSET and (sum(sub) / len(sub)) >= REQUIRE_CONV_BELOW:
                rescue = t
                break

        if rescue is not None:
            action = f"REQUIRE_CONV_{rescue}"
        elif hit < BLOCK_BELOW:
            action = "BLOCK"
        else:
            continue  # WATCH — underperforms but not gatable; hand rule (if any) stays
        rules.append((setup, regime, action, n, wins))
    return rules, relax


def derive_floors(sym_mfe: dict[str, list[float]]) -> dict[str, tuple[float, int, float]]:
    """symbol -> (calibrated_floor, n, pctile_raw). Clamped to [0.5x,2x] of the
    hand floor so a thin/odd sample can't blow the bar up or collapse it."""
    out: dict[str, tuple[float, int, float]] = {}
    for symbol, mfes in sym_mfe.items():
        n = len(mfes)
        if n < MIN_SAMPLE_FLOOR:
            continue
        hand = MIN_FAVORABLE_POINTS_PER_SYMBOL.get(symbol)
        if hand is None:
            continue
        raw = float(np.percentile(mfes, FLOOR_PCTILE))
        if raw <= 0.0:
            continue  # degenerate (mostly zero-MFE fires) — no usable suggestion
        lo, hi = hand * FLOOR_CLAMP[0], hand * FLOOR_CLAMP[1]
        clamped = max(lo, min(hi, raw))
        # round to a tidy number (1 sig digit-ish for index points)
        tidy = round(clamped, 2) if clamped < 1 else float(round(clamped))
        out[symbol] = (tidy, n, raw)
    return out


def factor_stats(factor_lift):
    """factor -> (n_present, hit_present, n_absent, hit_absent, lift_pct).
    Only factors with any data appear."""
    stats = {}
    for fac in ALL_FACTORS:
        d = factor_lift.get(fac)
        if not d:
            continue
        p, a = d[True], d[False]
        if not p and not a:
            continue
        hp = (sum(p) / len(p) * 100) if p else 0.0
        ha = (sum(a) / len(a) * 100) if a else 0.0
        stats[fac] = (len(p), hp, len(a), ha, hp - ha)
    return stats


def derive_conviction_weights(factor_lift) -> dict[str, int]:
    """factor -> weight, for factors measurable on BOTH sides (>= MIN_FACTOR_SAMPLE).

    weight = 2 if lift >= LIFT_W2, 1 if lift >= LIFT_W1, else 0. Factors without
    enough present/absent split are omitted so the hand weight stands.
    """
    weights: dict[str, int] = {}
    for fac, (np_, _hp, na, _ha, lift) in factor_stats(factor_lift).items():
        if np_ < MIN_FACTOR_SAMPLE or na < MIN_FACTOR_SAMPLE:
            continue
        weights[fac] = 2 if lift >= LIFT_W2 else (1 if lift >= LIFT_W1 else 0)
    return weights


# ----------------------------- EMIT -----------------------------

def write_calibration(rules, relax, conv_weights, floors, n_sessions, total_confirms, graded, ungraded):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        '"""AUTO-GENERATED by calibrate.py — do not edit by hand.',
        "",
        f"Re-derived {ts} from {n_sessions} session(s): {total_confirms} CONFIRMs,",
        f"{graded} graded / {ungraded} ungraded (insufficient forward window).",
        "",
        "core/config.py merges these over the hand-coded fallbacks at import time.",
        "Regenerate with: python calibrate.py",
        '"""',
        "from __future__ import annotations",
        "",
        "# (setup_label_exact, regime_substring, action). action in",
        '# {"BLOCK", "REQUIRE_CONV_<n>"}. Trailing comment = uncensored n and hit.',
        "CALIBRATED_REGIME_RULES: list[tuple[str, str, str]] = [",
    ]
    if rules:
        for setup, regime, action, n, wins in rules:
            hit = wins / n * 100
            lines.append(
                f'    ({setup!r:<22}, {regime!r:<16}, {action!r:<18}),'
                f'  # {wins}/{n} = {hit:.0f}%'
            )
    else:
        lines.append("    # (no pair cleared min-sample AND underperformed)")
    lines.append("]")
    lines.append("")
    lines.append("# (setup, regime) pairs that cleanly recovered (n >= MIN_SAMPLE_GATE")
    lines.append("# AND hit >= 40%). config.py DROPS any hand rule on these — that's how a")
    lines.append("# setup whose suppression is no longer justified gets un-gated. A merely")
    lines.append("# mediocre setup (WATCH band) is NOT here, so its hand block survives.")
    lines.append("CALIBRATED_RELAX: list[tuple[str, str]] = [")
    for setup, regime in relax:
        lines.append(f'    ({setup!r}, {regime!r}),')
    lines.append("]")
    lines.append("")
    lines.append("# Conviction factor weights overriding the hand defaults, derived from")
    lines.append("# each factor's realised MFE lift (only where present AND absent both")
    lines.append("# clear MIN_FACTOR_SAMPLE). config.py merges these — non-circular, so")
    lines.append("# auto-applied. Factors not here keep their hand weight.")
    lines.append("CALIBRATED_CONVICTION_WEIGHTS: dict[str, int] = {")
    for fac in ALL_FACTORS:
        if fac in conv_weights:
            lines.append(f'    {fac!r:<8}: {conv_weights[fac]},')
    lines.append("}")
    lines.append("")
    lines.append("# ADVISORY ONLY — config.py does NOT import these. Re-deriving the")
    lines.append("# floor from MFE magnitudes graded against that same floor is mildly")
    lines.append("# circular, so floors stay hand-set in config; this is a committed")
    lines.append("# reference to eyeball when deciding whether to hand-adjust them.")
    lines.append("# pctile of realised MFE, clamped to [0.5x, 2x] of the current floor.")
    lines.append("CALIBRATED_MFE_FLOORS: dict[str, float] = {")
    for symbol, (floor, n, raw) in sorted(floors.items()):
        lines.append(f'    {symbol!r:<14}: {floor!r:<8},  # n={n}, raw_p{FLOOR_PCTILE}={raw:.1f}')
    lines.append("}")
    lines.append("")
    content = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return content


# ----------------------------- REPORT -----------------------------

def print_report(population, rules, relax, factor_lift, conv_weights, floors, ungraded, total_confirms, n_sessions):
    print(f"\n{'='*70}")
    print(f"CALIBRATION  —  {n_sessions} sessions, {total_confirms} CONFIRMs "
          f"({total_confirms - ungraded} graded, {ungraded} ungraded)")
    print('='*70)

    print("\n(setup x regime) UNcensored hit-rate  [* = underperforms, n>=min]")
    print("  base = confirms + counterfactually-graded blocks; blk = #blocked")
    print(f"  {'setup':<22} {'regime':<14} {'n':>4} {'hit':>6} {'conv4+':>7} {'blk':>4}")
    for (setup, regime), fires in sorted(population.items(), key=lambda kv: -len(kv[1])):
        n = len(fires)
        wins = sum(w for _c, w, _b in fires)
        hit = wins / n * 100 if n else 0
        nblk = sum(1 for _c, _w, b in fires if b)
        sub = [w for c, w, b in fires if not b and c >= 4]
        sub_str = f"{sum(sub)/len(sub)*100:.0f}%/{len(sub)}" if sub else "-"
        flag = ""
        if n >= MIN_SAMPLE_GATE and hit < REQUIRE_CONV_BELOW * 100:
            flag = " *"
        thin = "" if n >= MIN_SAMPLE_GATE else "  (thin)"
        print(f"  {setup:<22} {regime:<14} {n:>4} {hit:>5.0f}% {sub_str:>7} {nblk:>4}{flag}{thin}")

    print("\nDERIVED GATE RULES:")
    if rules:
        for setup, regime, action, n, wins in rules:
            print(f"  {setup:<22} {regime:<14} -> {action:<16}  ({wins}/{n})")
    else:
        print("  (none — nothing cleared min-sample and underperformed)")

    # Diff vs the hand-coded fallback so the recommendation is explicit
    try:
        from core.config import HAND_REGIME_RULES
    except Exception:
        HAND_REGIME_RULES = []
    derived_map = {(s, r): a for s, r, a, _n, _w in rules}
    relax_set = set(relax)
    print("\nCHANGE vs HAND RULES:")
    changed = False
    for s, r, a in HAND_REGIME_RULES:
        if (s, r) in derived_map:
            if derived_map[(s, r)] != a:
                print(f"  CHANGE  {s} | {r}:  {a} -> {derived_map[(s, r)]}")
                changed = True
        elif (s, r) in relax_set:
            print(f"  RELAX   {s} | {r}:  {a} -> (none; recovered to >=40%)")
            changed = True
        else:
            print(f"  KEEP    {s} | {r}:  {a}  (thin or WATCH — not overridden)")
    for (s, r), a in derived_map.items():
        if not any(hs == s and hr == r for hs, hr, _ in HAND_REGIME_RULES):
            print(f"  ADD     {s} | {r}:  -> {a}")
            changed = True
    if not changed:
        print("  (no change to hand rules)")

    # Conviction factor lift + weight recommendation
    stats = factor_stats(factor_lift)
    print("\nCONVICTION FACTOR LIFT  (hit% present vs absent; EXH/CHOP/DIV")
    print("  reconstructed for pre-instrumentation sessions, rest need conv_factors)")
    print(f"  {'factor':<7} {'present':>12} {'absent':>12} {'lift':>6} {'hand':>5} {'->':>3} {'new':>4}")
    for fac in ALL_FACTORS:
        hand = HAND_CONVICTION_WEIGHTS.get(fac, 0)
        if fac not in stats:
            print(f"  {fac:<7} {'(no data)':>12} {'':>12} {'':>6} {hand:>5}   {'—':>4}")
            continue
        np_, hp, na, ha, lift = stats[fac]
        new = conv_weights.get(fac)
        new_str = str(new) if new is not None else "—"
        thin = "" if (np_ >= MIN_FACTOR_SAMPLE and na >= MIN_FACTOR_SAMPLE) else "  (thin)"
        print(f"  {fac:<7} {f'{hp:.0f}%/{np_}':>12} {f'{ha:.0f}%/{na}':>12} "
              f"{lift:>+5.0f} {hand:>5} {'->':>3} {new_str:>4}{thin}")

    print("\nDERIVED MFE FLOORS (advisory):")
    if floors:
        for symbol, (floor, n, raw) in sorted(floors.items()):
            hand = MIN_FAVORABLE_POINTS_PER_SYMBOL.get(symbol)
            print(f"  {symbol:<14} {hand} -> {floor}   (n={n}, raw_p{FLOOR_PCTILE}={raw:.1f})")
    else:
        print("  (none cleared min-sample)")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=None, help="only the last N sessions")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write calibration.py")
    args = ap.parse_args()

    files = list_event_files(args.last)
    if not files:
        print("No logs/*_events.csv found.")
        return 1

    population, sym_mfe, factor_lift, ungraded, total_confirms = collect(files)
    rules, relax = derive_rules(population)
    conv_weights = derive_conviction_weights(factor_lift)
    floors = derive_floors(sym_mfe)
    n_sessions = len(files)

    print_report(population, rules, relax, factor_lift, conv_weights, floors,
                 ungraded, total_confirms, n_sessions)

    if args.dry_run:
        print("--dry-run: core/calibration.py NOT written.")
        return 0

    graded = total_confirms - ungraded
    write_calibration(rules, relax, conv_weights, floors,
                      n_sessions, total_confirms, graded, ungraded)
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
