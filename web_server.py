"""
GammaLeak Web Server — FastAPI + WebSocket dashboard backend.

Runs the GammaLeak engine headless (no Rich TUI) and broadcasts all dashboard
state as JSON over WebSocket at 4 Hz.  Serves the static frontend from ./static/.

Usage:
    python web_server.py                   # default: 0.0.0.0:8080
    python web_server.py --port 3000       # custom port
    python web_server.py --mock            # mock mode (no Upstox token needed)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# Import the entire GammaLeak engine
# ---------------------------------------------------------------------------
import GammaLeak as engine

# ---------------------------------------------------------------------------
# State serializers live in ui/serializers.py — kept as a module reference so
# we can rebind ui.serializers._engine_ready from the bootloader below.
# ---------------------------------------------------------------------------
import ui.serializers as serializers
from ui.serializers import build_state_payload, _resolve_oi_chain_params

log = logging.getLogger("web_server")

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title="GammaLeak Dashboard")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------
_clients: set[WebSocket] = set()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    try:
        while True:
            # Keep connection alive; client sends pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)


async def broadcast_loop():
    """Push state to all connected clients at 4 Hz."""
    while True:
        if _clients:
            try:
                payload = json.dumps(build_state_payload())
            except Exception as exc:
                log.warning("Serialization error: %s", exc)
                await asyncio.sleep(0.25)
                continue
            dead = []
            for ws in _clients.copy():
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _clients.discard(ws)
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------
# Headless GammaLeak engine runner (no Rich TUI)
# ---------------------------------------------------------------------------

def _explicit_mock_mode(mock: bool) -> bool:
    if mock:
        return True
    override = os.environ.get("MOCK_MODE")
    if override is None:
        return False
    return override.strip().lower() in {"1", "true", "yes", "on"}


def _ensure_access_token() -> bool:
    current_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    needs_refresh = (
        not current_token
        or engine.looks_like_placeholder_token(current_token)
        or engine.is_token_expired(current_token)
    )
    if not needs_refresh:
        engine.ACCESS_TOKEN = current_token
        return True

    reason = "expired" if current_token and not engine.looks_like_placeholder_token(current_token) else "missing/invalid"
    log.warning("Token %s. Starting OAuth2 refresh...", reason)
    try:
        from oauth_token_exchange import get_fresh_access_token
    except ImportError:
        log.error("oauth_token_exchange module not found; cannot refresh Upstox token.")
        return False

    try:
        new_token = get_fresh_access_token()
    except Exception as exc:
        log.error("Token refresh error: %s", exc)
        return False

    if not new_token:
        log.error("Token refresh failed; engine will not start live mode.")
        return False

    engine.ACCESS_TOKEN = new_token
    os.environ["UPSTOX_ACCESS_TOKEN"] = new_token
    log.info("Token refreshed successfully")
    return True


async def run_engine_headless(mock: bool = False) -> None:
    """Boot and run the GammaLeak engine without the Rich Live dashboard."""

    if mock:
        os.environ["MOCK_MODE"] = "1"

    mock_mode = _explicit_mock_mode(mock)
    if not mock_mode and not _ensure_access_token():
        return

    await engine.resolve_dynamic_instruments()
    engine.reset_runtime_state()
    # Module-attribute rebind — flipping the local `_engine_ready` wouldn't
    # propagate to ui.serializers' copy. Going through the module reference
    # mutates the canonical flag the serializers actually read.
    serializers._engine_ready = True
    log.info("Engine ready — symbol_states initialized with %d instruments", len(engine.symbol_states))

    # Macro worker thread
    macro_worker = threading.Thread(target=engine.fetch_macro_worker, daemon=True)
    macro_worker.start()
    log.info("Macro worker thread started")

    # FII/DII pre-market fetch
    if engine.FII_BOOT_ENABLED and engine._FII_AVAILABLE:
        try:
            engine._fii_snapshot = await engine.fetch_latest_fii_data()
            log.info("FII/DII loaded: %s", engine._fii_snapshot.format_summary())
        except Exception as exc:
            log.warning("FII/DII fetch failed: %s", exc)

    # Sonar news engine
    if engine.SONAR_ENABLED and engine._SONAR_AVAILABLE:
        engine._sonar_engine = engine.SonarNewsEngine(cooldown_secs=engine.SONAR_COOLDOWN_SECS)
        if engine._sonar_engine.is_enabled:
            log.info("Sonar news engine initialized")
        else:
            engine._sonar_engine = None

    log_path = engine.get_log_dir()
    log_queue: asyncio.Queue = asyncio.Queue()
    tick_queue: asyncio.Queue = asyncio.Queue()

    if not mock_mode and not engine.ACCESS_TOKEN:
        log.error("No UPSTOX_ACCESS_TOKEN set.")
        return

    writer_task = asyncio.create_task(engine.disk_writer_task(log_queue, log_path))

    if mock_mode:
        feeder_task = asyncio.create_task(engine.mock_ws_task(log_queue))
        compute_task = None
    else:
        feeder_task = asyncio.create_task(engine.ws_task(log_queue, tick_queue))
        compute_task = asyncio.create_task(engine.compute_worker(tick_queue, log_queue))

    # Auto-roll task: checks for expiry changes daily at 08:55 IST
    roll_task = asyncio.create_task(engine.expiry_auto_roll())

    # Global indices poller — separate from main WS, REST-based, runs 24/7.
    from analytics.global_indices import poller_task as global_indices_poller
    global_indices_task = asyncio.create_task(global_indices_poller(interval_secs=30.0))

    # Full-chain OI poller — REST snapshot every 60s, dynamic ATM-relative windowing at render time.
    from orderflow.oi_chain import poller_task as oi_chain_poller
    oi_chain_task = asyncio.create_task(oi_chain_poller(_resolve_oi_chain_params, interval_secs=60.0))

    tasks = [writer_task, feeder_task, roll_task, global_indices_task, oi_chain_task]
    if compute_task is not None:
        tasks.append(compute_task)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def main(port: int, mock: bool, host: str = "127.0.0.1"):
    # Start GammaLeak engine in background
    engine_task = asyncio.create_task(run_engine_headless(mock))
    # Start broadcast loop
    bcast_task = asyncio.create_task(broadcast_loop())
    # Print a clickable URL (most terminals linkify http://localhost:port)
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"\n  Dashboard: http://{display_host}:{port}\n", flush=True)
    # Start FastAPI via uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    engine_task.cancel()
    bcast_task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GammaLeak Web Dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Bind address (default 127.0.0.1 for a clickable localhost URL; use 0.0.0.0 for LAN access)")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode (no API token needed)")
    args = parser.parse_args()
    asyncio.run(main(args.port, args.mock, args.host))
