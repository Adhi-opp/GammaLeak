"""Daily dealer-gamma sign anchor from NSE participant-wise OI.

The one number this module exists to produce:

    dealer_short_gamma_score ∈ [-1, +1]

  = Client net-long option contracts / total index-option OI.

Logic: an option HOLDER is long gamma regardless of call/put; the writer is
short it. NSE's participant file partitions every index-option contract into
Client (≈retail), DII, FII and Pro (≈prop/market-maker) long AND short sides,
so if the Client category is net LONG options, the professional complex on
the other side is net SHORT those options — i.e. dealers are short gamma and
their hedging amplifies moves. Positive score → dealers short gamma
(trend-friendly, fade-hostile); negative → dealers long gamma (pinning,
fade-friendly). This is measured position data, not the borrowed SPX sign
convention in orderflow/gex.py — once enough history exists the two get
reconciled by fit (proposal P2 layer 3).

Also derived (for the per-side GEX convention and the P9 flow prior):
per-category net calls / net puts / futures net.

Publication timing: NSE posts the file ~6-8pm IST for that trading day, so
the anchor available DURING day T is from day T-1. anchor_for() enforces
that lag — never hand it today's date expecting today's file.

History table: data/participant_history.csv, one row per trading day, raw
per-category sides + derived scores. Append-only via upsert_row(); the
backfill and the nightly scraper both write through it.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date

from fii_dii_scraper import FIISnapshot, ParticipantOI

HISTORY_PATH = os.path.join("data", "participant_history.csv")

_CATS = ("client", "dii", "fii", "pro")

# Raw sides persisted per category (suffix -> ParticipantOI attribute)
_RAW_FIELDS = (
    ("ocl", "opt_idx_call_long"),
    ("ocs", "opt_idx_call_short"),
    ("opl", "opt_idx_put_long"),
    ("ops", "opt_idx_put_short"),
    ("fil", "fut_idx_long"),
    ("fis", "fut_idx_short"),
)

HISTORY_COLUMNS = (
    ("date",)
    + tuple(f"{cat}_{suf}" for cat in _CATS for suf, _ in _RAW_FIELDS)
    + ("client_net_calls", "client_net_puts", "client_net_opt",
       "pro_net_opt", "total_opt_oi", "dealer_short_gamma_score")
)


@dataclass
class DealerAnchor:
    """Derived daily anchor consumed by the engine and studies."""
    as_of: date
    client_net_calls: int          # Client (CL - CS): retail net-long call contracts
    client_net_puts: int           # Client (PL - PS)
    client_net_opt: int            # sum of the two = customer net long-gamma contracts
    pro_net_opt: int               # Pro category net (dealer-proxy cross-check)
    total_opt_oi: int              # Σ all categories' option longs (= total OI)
    dealer_short_gamma_score: float  # client_net_opt / total_opt_oi, clamped [-1, 1]
    fii_fut_net: int = 0           # P9 prior: FII index-futures net
    dii_fut_net: int = 0
    client_fut_net: int = 0


def _net_opt(p: ParticipantOI | None) -> tuple[int, int, int]:
    """(net_calls, net_puts, net_total) for one category; zeros when absent."""
    if p is None:
        return 0, 0, 0
    nc = p.opt_idx_call_long - p.opt_idx_call_short
    np_ = p.opt_idx_put_long - p.opt_idx_put_short
    return nc, np_, nc + np_


def derive_anchor(snap: FIISnapshot) -> DealerAnchor:
    ccl, cpl, cnet = _net_opt(snap.client)
    _, _, pnet = _net_opt(snap.pro)
    total = 0
    for p in (snap.client, snap.dii, snap.fii, snap.pro):
        if p is not None:
            total += p.opt_idx_call_long + p.opt_idx_put_long
    score = 0.0
    if total > 0:
        score = max(-1.0, min(1.0, cnet / total))
    return DealerAnchor(
        as_of=snap.as_of_date,
        client_net_calls=ccl,
        client_net_puts=cpl,
        client_net_opt=cnet,
        pro_net_opt=pnet,
        total_opt_oi=total,
        dealer_short_gamma_score=score,
        fii_fut_net=snap.fii.fut_idx_net if snap.fii else 0,
        dii_fut_net=snap.dii.fut_idx_net if snap.dii else 0,
        client_fut_net=snap.client.fut_idx_net if snap.client else 0,
    )


def snapshot_to_row(snap: FIISnapshot) -> dict[str, str]:
    """Flatten one day's snapshot (raw sides + derived) into a history row."""
    row: dict[str, str] = {"date": snap.as_of_date.isoformat()}
    for cat in _CATS:
        p: ParticipantOI | None = getattr(snap, cat)
        for suf, attr in _RAW_FIELDS:
            row[f"{cat}_{suf}"] = str(getattr(p, attr)) if p is not None else ""
    a = derive_anchor(snap)
    row["client_net_calls"] = str(a.client_net_calls)
    row["client_net_puts"] = str(a.client_net_puts)
    row["client_net_opt"] = str(a.client_net_opt)
    row["pro_net_opt"] = str(a.pro_net_opt)
    row["total_opt_oi"] = str(a.total_opt_oi)
    row["dealer_short_gamma_score"] = f"{a.dealer_short_gamma_score:.6f}"
    return row


# ----------------------------- history table io -----------------------------

def load_history(path: str = HISTORY_PATH) -> dict[str, dict[str, str]]:
    """date-iso -> row dict. Empty when the table doesn't exist yet."""
    out: dict[str, dict[str, str]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("date") or "").strip()
                if d:
                    out[d] = row
    except FileNotFoundError:
        pass
    return out


def write_history(rows: dict[str, dict[str, str]], path: str = HISTORY_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(HISTORY_COLUMNS), extrasaction="ignore")
        w.writeheader()
        for d in sorted(rows):
            w.writerow({**{c: "" for c in HISTORY_COLUMNS}, **rows[d]})


def upsert_row(snap: FIISnapshot, path: str = HISTORY_PATH) -> None:
    rows = load_history(path)
    rows[snap.as_of_date.isoformat()] = snapshot_to_row(snap)
    write_history(rows, path)


def anchor_for(session_day: date, path: str = HISTORY_PATH) -> DealerAnchor | None:
    """Anchor usable DURING session_day = latest row strictly BEFORE it
    (publication lag — today's file appears only after today's close).
    Returns None when the table has no prior row."""
    rows = load_history(path)
    prior = [d for d in rows if d < session_day.isoformat()]
    if not prior:
        return None
    row = rows[max(prior)]
    try:
        return DealerAnchor(
            as_of=date.fromisoformat(row["date"]),
            client_net_calls=int(row["client_net_calls"] or 0),
            client_net_puts=int(row["client_net_puts"] or 0),
            client_net_opt=int(row["client_net_opt"] or 0),
            pro_net_opt=int(row["pro_net_opt"] or 0),
            total_opt_oi=int(row["total_opt_oi"] or 0),
            dealer_short_gamma_score=float(row["dealer_short_gamma_score"] or 0.0),
            fii_fut_net=int(row.get("fii_fil") or 0) - int(row.get("fii_fis") or 0),
            dii_fut_net=int(row.get("dii_fil") or 0) - int(row.get("dii_fis") or 0),
            client_fut_net=int(row.get("client_fil") or 0) - int(row.get("client_fis") or 0),
        )
    except (KeyError, ValueError):
        return None
