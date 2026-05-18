"""Log-file paths, CSV append, and the async disk-writer task.

The writer is per-symbol: every tick row is dispatched into
`logs/<date>/<symbol>.csv` based on the row's display-name column. On
startup, files whose existing header doesn't match the current LOG_COLUMNS
schema are archived with a timestamp suffix so the next flush re-creates
a fresh, correctly-headered file — keeps multi-day backtests from
silently mixing schema versions.
"""
from __future__ import annotations

import asyncio
import csv
import re
import time
from datetime import date, datetime
from pathlib import Path

# Imports point at the new package locations rather than reaching back into
# GammaLeak. The latter would trigger a pre-existing circular import
# when the engine runs as `__main__` (Python treats the file as two modules:
# `__main__` and `GammaLeak`, so re-importing it re-runs top-level
# code mid-load).
from core.config import EVENT_LOG_COLUMNS, IST, LOG_BATCH_SIZE, LOG_COLUMNS, LOG_DIR, LOG_FLUSH_INTERVAL_SECS
from core.state import console


LOG_STOP = object()  # sentinel value put on the queue to ask the writer to drain + exit


def _safe_filename(symbol: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", symbol)
    return safe or "_unknown"


def get_log_path(trading_day: date | None = None) -> Path:
    """Legacy single-file path. Kept for backwards-compat readers (replay / review fallback)."""
    active_day = trading_day or datetime.now(IST).date()
    return LOG_DIR / f"{active_day.isoformat()}.csv"


def get_log_dir(trading_day: date | None = None) -> Path:
    """Per-day folder containing one CSV per symbol — the current logging layout."""
    active_day = trading_day or datetime.now(IST).date()
    return LOG_DIR / active_day.isoformat()


def get_symbol_log_path(symbol: str, trading_day: date | None = None) -> Path:
    return get_log_dir(trading_day) / f"{_safe_filename(symbol)}.csv"


def get_events_log_path(trading_day: date | None = None) -> Path:
    active_day = trading_day or datetime.now(IST).date()
    return LOG_DIR / f"{active_day.isoformat()}_events.csv"


def append_event_row(
    timestamp: float, symbol: str, event_type: str, side: int,
    z_score: float, ltp: float, regime: str = "", setup_label: str = "",
    conviction: int = 0,
) -> None:
    """Append one sig_state transition to logs/YYYY-MM-DD_events.csv.

    Synchronous on purpose: transitions are low-rate (a handful per symbol per
    day) so the disk_writer_task batching machinery is overkill. Header is
    written lazily on first call of the day.
    """
    path = get_events_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    ts_ist = datetime.fromtimestamp(timestamp, IST).strftime("%Y-%m-%d %H:%M:%S")
    row = (
        f"{timestamp:.3f}", ts_ist, symbol, event_type, str(side),
        f"{z_score:.4f}", f"{ltp:.4f}", regime, setup_label, str(conviction),
    )
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(EVENT_LOG_COLUMNS)
        writer.writerow(row)


def append_csv_rows(
    log_path: Path, rows: list[tuple[str, ...]], write_header: bool
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(LOG_COLUMNS)
        writer.writerows(rows)
        handle.flush()


async def disk_writer_task(log_queue: asyncio.Queue, log_dir: Path) -> None:
    """Per-symbol dispatching writer. Each tick row goes to logs/<date>/<symbol>.csv.

    The 2nd element of every row (index [1]) is the display name from build_log_row,
    which is used as the dispatch key.

    On startup, scans existing per-symbol CSVs in log_dir. If the header doesn't
    match the current LOG_COLUMNS schema (radar restarted after a schema change),
    the stale file is renamed to <sym>.<ISO-timestamp>.csv so a fresh file gets
    created with the correct header on first flush. Prevents the
    12 / 16 / 20-column drift problem from polluting backtests.
    """
    console.log(f"Writer task started, logging to {log_dir}/")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        console.log(f"[red]ERROR creating log dir: {exc}[/red]")
        return

    # Schema sentinel: rotate any pre-existing CSV whose header doesn't match
    # the current LOG_COLUMNS. Only touch the canonical per-symbol filenames
    # (no dots in stem); leave already-archived files alone.
    expected_header = list(LOG_COLUMNS)
    suffix = datetime.now(IST).strftime("%Y-%m-%dT%H-%M-%S")
    for path in sorted(log_dir.glob("*.csv")):
        if "." in path.stem:
            continue  # already archived (e.g., NIFTY_FUT.2026-05-11T14-22-00.csv)
        try:
            with path.open("r", encoding="utf-8") as f:
                existing_header = next(csv.reader(f), [])
        except Exception as exc:
            console.log(f"[yellow]Could not read header of {path.name}: {exc}[/yellow]")
            continue
        if existing_header == expected_header:
            console.log(f"[dim]{path.name}: schema OK, appending[/dim]")
            continue
        archived = log_dir / f"{path.stem}.{suffix}{path.suffix}"
        try:
            path.rename(archived)
            console.log(
                f"[yellow]Schema mismatch on {path.name} "
                f"(existing={len(existing_header)} cols, expected={len(expected_header)}); "
                f"archived -> {archived.name}[/yellow]"
            )
        except Exception as exc:
            console.log(f"[red]Failed to archive {path.name}: {exc}[/red]")

    pending_by_symbol: dict[str, list[tuple[str, ...]]] = {}
    last_flush = time.monotonic()
    flush_interval = min(LOG_FLUSH_INTERVAL_SECS, 5.0)

    def _flush_all() -> int:
        flushed = 0
        for sym, rows in list(pending_by_symbol.items()):
            if not rows:
                continue
            path = log_dir / f"{_safe_filename(sym)}.csv"
            write_header = not path.exists()
            append_csv_rows(path, rows, write_header)
            flushed += len(rows)
        pending_by_symbol.clear()
        return flushed

    try:
        while True:
            timeout = max(0.1, flush_interval - (time.monotonic() - last_flush))
            try:
                queue_item = await asyncio.wait_for(log_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                queue_item = None

            if queue_item is LOG_STOP:
                if any(pending_by_symbol.values()):
                    try:
                        n = await asyncio.to_thread(_flush_all)
                        console.log(
                            f"[green][FINAL FLUSH] Wrote {n} rows across "
                            f"{len(pending_by_symbol)} symbols to {log_dir}/[/green]"
                        )
                    except Exception as exc:
                        console.log(f"[red]ERROR final flush: {exc}[/red]")
                return

            if queue_item is not None:
                sym = queue_item[1] if len(queue_item) > 1 else "_unknown"
                pending_by_symbol.setdefault(sym, []).append(queue_item)

            total_pending = sum(len(v) for v in pending_by_symbol.values())
            time_since_flush = time.monotonic() - last_flush
            if total_pending and (
                queue_item is None
                or total_pending >= LOG_BATCH_SIZE
                or time_since_flush >= flush_interval
            ):
                try:
                    await asyncio.to_thread(_flush_all)
                except Exception as exc:
                    console.log(f"[red]ERROR flush: {exc}[/red]")
                last_flush = time.monotonic()
    except asyncio.CancelledError:
        if any(pending_by_symbol.values()):
            try:
                n = _flush_all()
                console.log(f"[yellow]Emergency flush: {n} rows saved[/yellow]")
            except Exception:
                pass
        raise
    except Exception as exc:
        console.log(f"[red]Writer task CRASHED: {exc}[/red]")
        if any(pending_by_symbol.values()):
            try:
                _flush_all()
            except Exception:
                pass
