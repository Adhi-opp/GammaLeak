"""
FII/DII Participant-wise OI Scraper
====================================
Downloads NSE's daily F&O participant OI CSV and extracts FII/DII positioning.

URL pattern: https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv

Usage:
  # As standalone script (pre-market check):
  python fii_dii_scraper.py
  python fii_dii_scraper.py --date 2026-04-07
  python fii_dii_scraper.py --days 5          # Last 5 trading days

  # As module (imported by GammaLeak):
  from fii_dii_scraper import fetch_latest_fii_data, FIISnapshot
"""

import argparse
import csv
import io
import os
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

IST = timezone(timedelta(hours=5, minutes=30))

UPSTOX_FII_URL = "https://api.upstox.com/v2/market/fii"
UPSTOX_DII_URL = "https://api.upstox.com/v2/market/dii"
UPSTOX_FII_SEGMENTS = (
    "NSE_FO|INDEX_FUTURES",
    "NSE_FO|STOCK_FUTURES",
    "NSE_FO|INDEX_OPTIONS",
    "NSE_FO|STOCK_OPTIONS",
)
UPSTOX_CASH_SEGMENT = "NSE_EQ|CASH"

# NSE requires browser-like headers or it rejects with 403
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

NSE_BASE_URL = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date_str}.csv"
NSE_ALT_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{date_str}.csv"

# Column indices in the CSV (0-indexed, after "Client Type")
COL_MAP = {
    "fut_idx_long": 1,
    "fut_idx_short": 2,
    "fut_stk_long": 3,
    "fut_stk_short": 4,
    "opt_idx_call_long": 5,
    "opt_idx_put_long": 6,
    "opt_idx_call_short": 7,
    "opt_idx_put_short": 8,
    "opt_stk_call_long": 9,
    "opt_stk_put_long": 10,
    "opt_stk_call_short": 11,
    "opt_stk_put_short": 12,
    "total_long": 13,
    "total_short": 14,
}


@dataclass
class ParticipantOI:
    """OI breakdown for a single participant type."""
    client_type: str

    fut_idx_long: int = 0
    fut_idx_short: int = 0
    fut_stk_long: int = 0
    fut_stk_short: int = 0
    opt_idx_call_long: int = 0
    opt_idx_put_long: int = 0
    opt_idx_call_short: int = 0
    opt_idx_put_short: int = 0
    opt_stk_call_long: int = 0
    opt_stk_put_long: int = 0
    opt_stk_call_short: int = 0
    opt_stk_put_short: int = 0
    total_long: int = 0
    total_short: int = 0

    @property
    def fut_idx_net(self) -> int:
        return self.fut_idx_long - self.fut_idx_short

    @property
    def opt_idx_net(self) -> int:
        """Net index options = (call_long + put_short) - (call_short + put_long)
        Positive = bullish positioning."""
        return (
            (self.opt_idx_call_long + self.opt_idx_put_short)
            - (self.opt_idx_call_short + self.opt_idx_put_long)
        )

    @property
    def total_net(self) -> int:
        return self.total_long - self.total_short


@dataclass
class CashFlow:
    """Cash-segment buy/sell flow in INR crores (from Upstox /market/{fii,dii})."""
    buy_amount: float = 0.0
    sell_amount: float = 0.0

    @property
    def net(self) -> float:
        return self.buy_amount - self.sell_amount


@dataclass
class FIISnapshot:
    """Processed FII/DII positioning snapshot for GammaLeak consumption."""
    as_of_date: date

    fii: ParticipantOI | None = None
    dii: ParticipantOI | None = None
    pro: ParticipantOI | None = None
    client: ParticipantOI | None = None
    # Cash-market flows (Upstox only; NSE participant CSV is F&O only).
    fii_cash: CashFlow | None = None
    dii_cash: CashFlow | None = None

    @property
    def fii_fut_idx_net(self) -> int:
        return self.fii.fut_idx_net if self.fii else 0

    @property
    def fii_opt_idx_net(self) -> int:
        return self.fii.opt_idx_net if self.fii else 0

    @property
    def dii_fut_idx_net(self) -> int:
        return self.dii.fut_idx_net if self.dii else 0

    @property
    def fii_bias(self) -> str:
        """Simple FII bias label based on index futures net."""
        net = self.fii_fut_idx_net
        if net > 10000:
            return "STRONG LONG"
        elif net > 0:
            return "LONG"
        elif net > -10000:
            return "SHORT"
        else:
            return "STRONG SHORT"

    @property
    def dii_bias(self) -> str:
        net = self.dii_fut_idx_net
        if net > 10000:
            return "STRONG LONG"
        elif net > 0:
            return "LONG"
        elif net > -10000:
            return "SHORT"
        else:
            return "STRONG SHORT"

    def format_summary(self) -> str:
        """One-line summary for GammaLeak macro header."""
        if not self.fii:
            return f"FII/DII: No data ({self.as_of_date})"

        fii_net = self.fii_fut_idx_net
        dii_net = self.dii_fut_idx_net
        fii_opt = self.fii_opt_idx_net

        fii_sign = "+" if fii_net >= 0 else ""
        dii_sign = "+" if dii_net >= 0 else ""
        fii_opt_sign = "+" if fii_opt >= 0 else ""

        parts = [
            f"FII Fut: {fii_sign}{fii_net:,} ({self.fii_bias})",
            f"FII Opt: {fii_opt_sign}{fii_opt:,}",
            f"DII Fut: {dii_sign}{dii_net:,} ({self.dii_bias})",
        ]
        if self.fii_cash is not None:
            s = "+" if self.fii_cash.net >= 0 else ""
            parts.append(f"FII Cash: {s}Rs {self.fii_cash.net:,.0f} cr")
        if self.dii_cash is not None:
            s = "+" if self.dii_cash.net >= 0 else ""
            parts.append(f"DII Cash: {s}Rs {self.dii_cash.net:,.0f} cr")
        parts.append(f"Date: {self.as_of_date}")
        return "  |  ".join(parts)

    def format_detail(self) -> str:
        """Multi-line detail for standalone display."""
        lines = [f"NSE F&O Participant OI - {self.as_of_date}", "=" * 60]
        for label, p in [("FII", self.fii), ("DII", self.dii), ("PRO", self.pro), ("CLIENT", self.client)]:
            if not p:
                continue
            lines.append(f"\n{label}:")
            lines.append(f"  Index Futures  : Long {p.fut_idx_long:>10,}  Short {p.fut_idx_short:>10,}  Net {p.fut_idx_net:>+10,}")
            lines.append(f"  Stock Futures  : Long {p.fut_stk_long:>10,}  Short {p.fut_stk_short:>10,}")
            lines.append(f"  Index Options  : Call L {p.opt_idx_call_long:>10,}  Put L {p.opt_idx_put_long:>10,}")
            lines.append(f"                   Call S {p.opt_idx_call_short:>10,}  Put S {p.opt_idx_put_short:>10,}  Net {p.opt_idx_net:>+10,}")
            lines.append(f"  Total          : Long {p.total_long:>10,}  Short {p.total_short:>10,}  Net {p.total_net:>+10,}")
        return "\n".join(lines)


def _parse_csv(text: str, target_date: date) -> FIISnapshot:
    """Parse NSE participant OI CSV text into FIISnapshot."""
    reader = csv.reader(io.StringIO(text.strip()))
    rows = list(reader)

    # Skip header row
    if not rows:
        raise ValueError("Empty CSV response")

    snapshot = FIISnapshot(as_of_date=target_date)

    type_map = {
        "Client": "client",
        "DII": "dii",
        "FII": "fii",
        "Pro": "pro",
    }

    for row in rows:
        if not row or len(row) < 15:
            continue
        client_type = row[0].strip()
        if client_type not in type_map:
            continue

        vals = []
        for i in range(1, 15):
            try:
                vals.append(int(row[i].strip().replace(",", "")))
            except (ValueError, IndexError):
                vals.append(0)

        p = ParticipantOI(
            client_type=client_type,
            fut_idx_long=vals[0],
            fut_idx_short=vals[1],
            fut_stk_long=vals[2],
            fut_stk_short=vals[3],
            opt_idx_call_long=vals[4],
            opt_idx_put_long=vals[5],
            opt_idx_call_short=vals[6],
            opt_idx_put_short=vals[7],
            opt_stk_call_long=vals[8],
            opt_stk_put_long=vals[9],
            opt_stk_call_short=vals[10],
            opt_stk_put_short=vals[11],
            total_long=vals[12],
            total_short=vals[13],
        )
        setattr(snapshot, type_map[client_type], p)

    if not snapshot.fii:
        raise ValueError("FII row not found in CSV")

    return snapshot


async def _fetch_csv(session: aiohttp.ClientSession, target_date: date) -> str:
    """Download participant OI CSV for a given date. Tries both NSE archive URLs."""
    date_str = target_date.strftime("%d%m%Y")

    for base_url in [NSE_BASE_URL, NSE_ALT_URL]:
        url = base_url.format(date_str=date_str)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "Client Type" in text or "FII" in text:
                        return text
                # 404 = no data for this date (weekend/holiday), try alt URL
        except (aiohttp.ClientError, TimeoutError):
            continue

    raise FileNotFoundError(f"No participant OI data available for {target_date}")


def _get_last_trading_date() -> date:
    """Get the most recent likely trading date (skip weekends)."""
    today = datetime.now(IST).date()
    # If before 8 PM IST, NSE may not have published today's data yet
    now_ist = datetime.now(IST)
    if now_ist.hour < 20:
        today = today - timedelta(days=1)

    # Skip weekends
    while today.weekday() >= 5:  # Saturday=5, Sunday=6
        today -= timedelta(days=1)

    return today


async def fetch_fii_snapshot(target_date: date | None = None) -> FIISnapshot:
    """Fetch and parse FII/DII OI for a specific date.

    Args:
        target_date: Date to fetch. If None, uses last trading date.

    Returns:
        FIISnapshot with parsed positioning data.
    """
    if target_date is None:
        target_date = _get_last_trading_date()

    ssl_ctx = ssl.create_default_context()
    conn = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(headers=NSE_HEADERS, connector=conn) as session:
        # First hit NSE homepage to get cookies (NSE blocks direct CSV access without session)
        try:
            async with session.get(
                "https://www.nseindia.com/", timeout=aiohttp.ClientTimeout(total=10)
            ):
                pass
        except Exception:
            pass  # Cookie prefetch is best-effort

        text = await _fetch_csv(session, target_date)
        return _parse_csv(text, target_date)


async def fetch_upstox_fii_only() -> tuple[ParticipantOI, CashFlow | None, date] | None:
    # Hybrid path: fetch FII positioning + cash flow from Upstox /market/fii in a
    # single multi-segment call. Returns (ParticipantOI for F&O, CashFlow for
    # NSE_EQ|CASH or None if cash segment missing, trading-date). None overall
    # if no token, request fails, or INDEX_FUTURES is missing — caller falls back to NSE.
    # Note: Upstox /market/dii is cash-only; DII F&O positioning still comes from NSE.
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        return None

    segments = (*UPSTOX_FII_SEGMENTS, UPSTOX_CASH_SEGMENT)
    params = [("data_type", seg) for seg in segments]
    params.append(("interval", "1D"))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                UPSTOX_FII_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None

    data = payload.get("data") or {}

    def latest(seg: str) -> dict:
        rows = data.get(seg) or []
        return rows[0] if rows else {}

    idx_fut = latest("NSE_FO|INDEX_FUTURES")
    stk_fut = latest("NSE_FO|STOCK_FUTURES")
    idx_opt = latest("NSE_FO|INDEX_OPTIONS")
    stk_opt = latest("NSE_FO|STOCK_OPTIONS")
    cash = latest(UPSTOX_CASH_SEGMENT)

    if not idx_fut:
        return None

    p = ParticipantOI(
        client_type="FII",
        fut_idx_long=int(idx_fut.get("total_long_contracts", 0) or 0),
        fut_idx_short=int(idx_fut.get("total_short_contracts", 0) or 0),
        fut_stk_long=int(stk_fut.get("total_long_contracts", 0) or 0),
        fut_stk_short=int(stk_fut.get("total_short_contracts", 0) or 0),
        opt_idx_call_long=int(idx_opt.get("total_call_long_contracts", 0) or 0),
        opt_idx_put_long=int(idx_opt.get("total_put_long_contracts", 0) or 0),
        opt_idx_call_short=int(idx_opt.get("total_call_short_contracts", 0) or 0),
        opt_idx_put_short=int(idx_opt.get("total_put_short_contracts", 0) or 0),
        opt_stk_call_long=int(stk_opt.get("total_call_long_contracts", 0) or 0),
        opt_stk_put_long=int(stk_opt.get("total_put_long_contracts", 0) or 0),
        opt_stk_call_short=int(stk_opt.get("total_call_short_contracts", 0) or 0),
        opt_stk_put_short=int(stk_opt.get("total_put_short_contracts", 0) or 0),
        total_long=0,
        total_short=0,
    )

    cash_flow: CashFlow | None = None
    if cash:
        cash_flow = CashFlow(
            buy_amount=float(cash.get("buy_amount", 0) or 0),
            sell_amount=float(cash.get("sell_amount", 0) or 0),
        )

    ts_ms = int(idx_fut.get("time_stamp", 0) or 0)
    as_of = datetime.fromtimestamp(ts_ms / 1000, tz=IST).date() if ts_ms else _get_last_trading_date()
    return p, cash_flow, as_of


async def fetch_upstox_dii_cash() -> tuple[CashFlow, date] | None:
    # /market/dii only supports NSE_EQ|CASH (server enforces this, UDAPI1201 otherwise).
    # Returns DII cash buy/sell amounts in ₹ crores. None on any failure — caller continues.
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        return None

    params = [("data_type", UPSTOX_CASH_SEGMENT), ("interval", "1D")]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                UPSTOX_DII_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None

    rows = (payload.get("data") or {}).get(UPSTOX_CASH_SEGMENT) or []
    if not rows:
        return None
    row = rows[0]
    flow = CashFlow(
        buy_amount=float(row.get("buy_amount", 0) or 0),
        sell_amount=float(row.get("sell_amount", 0) or 0),
    )
    ts_ms = int(row.get("time_stamp", 0) or 0)
    as_of = datetime.fromtimestamp(ts_ms / 1000, tz=IST).date() if ts_ms else _get_last_trading_date()
    return flow, as_of


async def _fetch_nse_snapshot(lookback_days: int) -> FIISnapshot | None:
    ssl_ctx = ssl.create_default_context()
    conn = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(headers=NSE_HEADERS, connector=conn) as session:
        try:
            async with session.get(
                "https://www.nseindia.com/", timeout=aiohttp.ClientTimeout(total=10)
            ):
                pass
        except Exception:
            pass

        base_date = _get_last_trading_date()
        for offset in range(lookback_days):
            candidate = base_date - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            try:
                text = await _fetch_csv(session, candidate)
                return _parse_csv(text, candidate)
            except (FileNotFoundError, ValueError):
                continue
    return None


async def fetch_latest_fii_data(lookback_days: int = 5) -> FIISnapshot:
    """Try fetching FII/DII data for the most recent trading day, with fallback.

    Hybrid source: Upstox is preferred for FII F&O + cash flows (cleaner, richer);
    NSE always provides DII/PRO/CLIENT F&O positioning (Upstox doesn't expose
    DII F&O). DII cash flows come from Upstox /market/dii. All three sources are
    fetched in parallel — if one fails, we still return whatever the others gave.
    """
    import asyncio
    upstox_fii_task = asyncio.create_task(fetch_upstox_fii_only())
    upstox_dii_task = asyncio.create_task(fetch_upstox_dii_cash())
    nse_task = asyncio.create_task(_fetch_nse_snapshot(lookback_days))
    upstox_fii_res, upstox_dii_res, nse_snapshot = await asyncio.gather(
        upstox_fii_task, upstox_dii_task, nse_task
    )

    if nse_snapshot is not None:
        if upstox_fii_res is not None:
            fii_p, fii_cash, _ = upstox_fii_res
            nse_snapshot.fii = fii_p
            nse_snapshot.fii_cash = fii_cash
        if upstox_dii_res is not None:
            dii_cash, _ = upstox_dii_res
            nse_snapshot.dii_cash = dii_cash
        return nse_snapshot

    # NSE failed — assemble whatever Upstox gave us
    if upstox_fii_res is not None or upstox_dii_res is not None:
        if upstox_fii_res is not None:
            fii_p, fii_cash, as_of = upstox_fii_res
        else:
            fii_p, fii_cash = None, None
            as_of = upstox_dii_res[1] if upstox_dii_res else _get_last_trading_date()
        dii_cash = upstox_dii_res[0] if upstox_dii_res is not None else None
        return FIISnapshot(
            as_of_date=as_of,
            fii=fii_p,
            fii_cash=fii_cash,
            dii_cash=dii_cash,
        )

    raise FileNotFoundError(f"No FII/DII data found in last {lookback_days} trading days")


async def fetch_multi_day(days: int = 5) -> list[FIISnapshot]:
    """Fetch multiple days of FII data for trend analysis."""
    ssl_ctx = ssl.create_default_context()
    conn = aiohttp.TCPConnector(ssl=ssl_ctx)
    results = []

    async with aiohttp.ClientSession(headers=NSE_HEADERS, connector=conn) as session:
        try:
            async with session.get(
                "https://www.nseindia.com/", timeout=aiohttp.ClientTimeout(total=10)
            ):
                pass
        except Exception:
            pass

        base_date = _get_last_trading_date()
        checked = 0
        offset = 0

        while checked < days and offset < days + 10:
            candidate = base_date - timedelta(days=offset)
            offset += 1
            if candidate.weekday() >= 5:
                continue
            try:
                text = await _fetch_csv(session, candidate)
                results.append(_parse_csv(text, candidate))
                checked += 1
            except (FileNotFoundError, ValueError):
                checked += 1  # Count attempts, not just successes

    return results


# ---------- CLI ----------

async def _cli_main(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    if args.date:
        target = date.fromisoformat(args.date)
        console.print(f"[cyan]Fetching FII/DII OI for {target}...[/cyan]")
        snapshot = await fetch_fii_snapshot(target)
        console.print(Panel(snapshot.format_detail(), title="Participant OI", border_style="green"))
        console.print(f"\n[bold]{snapshot.format_summary()}[/bold]")
    elif args.days:
        console.print(f"[cyan]Fetching last {args.days} trading days...[/cyan]")
        snapshots = await fetch_multi_day(args.days)
        for snap in snapshots:
            console.print(f"\n[bold]{snap.as_of_date}[/bold]: {snap.format_summary()}")
        if len(snapshots) >= 2:
            latest = snapshots[0]
            prev = snapshots[1]
            if latest.fii and prev.fii:
                delta = latest.fii.fut_idx_net - prev.fii.fut_idx_net
                sign = "+" if delta >= 0 else ""
                console.print(f"\n[yellow]FII Index Futures Net Change (1d): {sign}{delta:,}[/yellow]")
    else:
        console.print("[cyan]Fetching latest FII/DII OI...[/cyan]")
        snapshot = await fetch_latest_fii_data()
        console.print(Panel(snapshot.format_detail(), title="Participant OI", border_style="green"))
        console.print(f"\n[bold]{snapshot.format_summary()}[/bold]")


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="NSE FII/DII Participant OI Scraper")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Specific date to fetch")
    parser.add_argument("--days", type=int, default=0, help="Fetch last N trading days")
    args = parser.parse_args()

    asyncio.run(_cli_main(args))
