"""Closing Auction Session (CAS) handling — session clock, tracker, engine
freeze guard, grader truncation, multi-venue contract resolution.

Stdlib unittest (no new dependency):
    python -m unittest discover -s tests -v

Every test redirects log writes to a temp dir; nothing touches logs/.
"""
from __future__ import annotations

import asyncio
import csv
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import gammaleak_runtime.io_logs as io_logs  # noqa: E402
from core.config import (  # noqa: E402
    EXPIRY_BLACKOUT_END,
    FNO_CLOSE,
    IST,
    cas_phase,
    continuous_session_end,
    session_phase,
)

CAS_DAY = date(2026, 9, 10)       # a Thursday inside the verified CAS sample
PRE_CAS_DAY = date(2026, 7, 3)


def ist(d: date, h: int, m: int, s: float = 0.0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST) + timedelta(seconds=s)


class _TempLogs(unittest.TestCase):
    """Point every log writer at a throwaway directory."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="gl_test_"))
        self._old_log_dir = io_logs.LOG_DIR
        io_logs.LOG_DIR = self._tmp

    def tearDown(self) -> None:
        io_logs.LOG_DIR = self._old_log_dir
        shutil.rmtree(self._tmp, ignore_errors=True)


# --------------------------------------------------------------------- clock

class TestSessionClock(unittest.TestCase):
    def test_phases_are_era_aware(self):
        self.assertEqual(session_phase(ist(PRE_CAS_DAY, 15, 20)), "CLOSE")
        self.assertEqual(session_phase(ist(CAS_DAY, 15, 14)), "CLOSE")
        self.assertEqual(session_phase(ist(CAS_DAY, 15, 15)), "CAS")
        self.assertEqual(session_phase(ist(CAS_DAY, 15, 39)), "CAS")
        self.assertEqual(session_phase(ist(CAS_DAY, 15, 40)), "")

    def test_cas_sub_phases(self):
        expect = {(15, 0): "REFERENCE", (15, 15): "TRANSITION", (15, 20): "ORDER_ENTRY",
                  (15, 25): "LIMIT_ONLY", (15, 28): "RANDOM_CLOSE", (15, 30): "MATCHING",
                  (15, 35): "POST_CAS_FNO", (15, 40): "", (14, 59): ""}
        for (h, m), label in expect.items():
            self.assertEqual(cas_phase(ist(CAS_DAY, h, m)), label, f"{h}:{m}")
        self.assertEqual(cas_phase(ist(PRE_CAS_DAY, 15, 20)), "")

    def test_continuous_session_end_by_class(self):
        self.assertEqual(continuous_session_end("NIFTY", CAS_DAY), (15, 15))
        self.assertEqual(continuous_session_end("HDFCBANK", CAS_DAY), (15, 15))
        self.assertEqual(continuous_session_end("NIFTY_FUT", CAS_DAY), (15, 40))
        self.assertEqual(continuous_session_end("NIFTY", PRE_CAS_DAY), (15, 30))
        self.assertEqual(continuous_session_end("BN_FUT", PRE_CAS_DAY), (15, 30))
        for own_session in ("CRUDEOIL", "VIX"):
            self.assertIsNone(continuous_session_end(own_session, CAS_DAY))

    def test_expiry_blackout_runs_to_fno_close(self):
        self.assertEqual(EXPIRY_BLACKOUT_END, FNO_CLOSE)


# --------------------------------------------------------------------- tracker

class TestCASTracker(_TempLogs):
    def setUp(self) -> None:
        super().setUp()
        from core.models import CASIndexState
        from core.state import cas_state, pcr_state
        import orderflow.cas as cas
        self.cas, self.state = cas, cas_state
        for k in list(cas_state):
            cas_state[k] = CASIndexState()
        pcr_state.ce_ltp_by_strike.clear(); pcr_state.pe_ltp_by_strike.clear()
        pcr_state.ce_ltp_ts_by_strike.clear(); pcr_state.pe_ltp_ts_by_strike.clear()
        cas._last_sample_ts = 0.0
        cas._last_synth_ts = 0.0

    def _spot(self, px, h, m, s=0.0, index="BANKNIFTY"):
        t = ist(CAS_DAY, h, m, s)
        self.cas.on_spot_tick(index, px, t.timestamp(), t)

    def _fut(self, px, h, m, s=0.0, disp="BN_FUT"):
        t = ist(CAS_DAY, h, m, s)
        self.cas.on_fut_tick(disp, px, t.timestamp(), t)

    def test_freeze_print_and_futures_implied_close(self):
        self._fut(56400.0, 15, 14, 50)
        self._spot(56262.0, 15, 14, 55)               # last continuous print
        self._spot(56262.5, 15, 15, 0)                # the 15:15 freeze value
        self._fut(56500.0, 15, 22)                    # futures drift +100 inside the auction
        for sec in range(0, 600, 30):                 # heartbeat ticks at the frozen value
            self._spot(56262.5, 15, 16, sec)
        s = self.state["BANKNIFTY"]
        self.assertEqual(s.frozen, 56262.5)
        self.assertEqual(s.official_close, 0.0)       # not printed yet
        self.assertAlmostEqual(s.basis_cts, 56400.0 - 56262.0)
        self.assertAlmostEqual(s.fut_implied_close, 56500.0 - 138.0)
        self.assertEqual(s.implied_src, "FUT")
        lock = s.implied_close_at_lock
        self._spot(56471.95, 15, 28, 40)              # the official close prints
        self.assertEqual(s.official_close, 56471.95)
        self.assertEqual(s.implied_close_at_lock, lock, "the scored forecast must predate the print")
        est = self.cas.settlement_estimate("BANKNIFTY", ist(CAS_DAY, 15, 29))
        self.assertEqual(est, (56471.95, "CLOSE"))
        with open(self._tmp / f"{CAS_DAY}_cas.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        summary = [r for r in rows if r["row_type"] == "SUMMARY" and r["index"] == "BANKNIFTY"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["realized_gap"], f"{56471.95 - 56262.5:+.2f}")

    def test_unchanged_close_resolves_at_matching_end(self):
        self._spot(23400.0, 15, 14, 59, index="NIFTY")
        self._spot(23400.0, 15, 15, 1, index="NIFTY")
        self._spot(23400.0, 15, 35, 2, index="NIFTY")
        s = self.state["NIFTY"]
        self.assertEqual(s.official_close, 23400.0)

    def test_restart_after_print_leaves_freeze_unknown(self):
        self._spot(23477.8, 15, 29, 10, index="NIFTY")   # first tick seen is the close
        s = self.state["NIFTY"]
        self.assertEqual(s.frozen, 0.0)
        self.assertEqual(s.official_close, 0.0)
        self.assertIsNone(self.cas.settlement_estimate("NIFTY", ist(CAS_DAY, 15, 30)))

    def test_parity_forward_uses_fresh_quotes_only(self):
        from core.state import pcr_state
        now = ist(CAS_DAY, 15, 22).timestamp()
        quotes = {23350: (120.0, 80.0), 23400: (90.0, 101.0), 23450: (60.0, 120.0)}
        for k, (c, p) in quotes.items():
            pcr_state.ce_ltp_by_strike[k], pcr_state.pe_ltp_by_strike[k] = c, p
            pcr_state.ce_ltp_ts_by_strike[k] = pcr_state.pe_ltp_ts_by_strike[k] = now - 5
        # K + C - P: 23390, 23389, 23390 -> median 23390
        self.assertEqual(self.cas.parity_forward(now, 23395.0), 23390.0)
        pcr_state.pe_ltp_ts_by_strike[23400] = now - 600          # stale put leg
        pcr_state.ce_ltp_by_strike[23500], pcr_state.pe_ltp_by_strike[23500] = 40.0, 150.0
        pcr_state.ce_ltp_ts_by_strike[23500] = pcr_state.pe_ltp_ts_by_strike[23500] = now - 5
        # 23400 dropped; nearest fresh three: 23350, 23450, 23500 -> 23390, 23390, 23390
        self.assertEqual(self.cas.parity_forward(now, 23395.0), 23390.0)

    def test_market_status_logs_transitions_only(self):
        from core.state import market_status
        market_status.clear()
        t = ist(CAS_DAY, 15, 15).timestamp()
        self.cas.on_market_info({"NSE_EQ": "NORMAL_OPEN", "NSE_FO": "NORMAL_OPEN"}, t)
        self.cas.on_market_info({"NSE_EQ": "NORMAL_OPEN", "NSE_FO": "NORMAL_OPEN"}, t + 1)
        self.cas.on_market_info({"NSE_EQ": "CLOSING_START", "NSE_FO": "NORMAL_OPEN"}, t + 2)
        with open(self._tmp / f"{CAS_DAY}_market_status.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([(r["segment"], r["status"]) for r in rows],
                         [("NSE_EQ", "NORMAL_OPEN"), ("NSE_FO", "NORMAL_OPEN"), ("NSE_EQ", "CLOSING_START")])


# --------------------------------------------------------------------- engine

class TestEngineFreezeGuard(_TempLogs):
    """A CAS day replayed through record_tick: before the fix, the frozen
    index drove |Z| to the cap and the close print produced ALERT and
    TREND_KILL rows (sim on 2026-08-03 / 09-10 / 09-30 bars)."""

    KEY = "NSE_INDEX|Nifty 50"

    def setUp(self) -> None:
        super().setUp()
        import GammaLeak as engine
        self.engine = engine
        self.events: list[dict] = []
        self._orig_append = engine.append_event_row
        engine.append_event_row = lambda **kw: self.events.append(kw)
        engine.symbol_states.clear()
        engine.symbol_states[self.KEY] = engine.SymbolState()

    def tearDown(self) -> None:
        self.engine.append_event_row = self._orig_append
        self.engine.symbol_states.clear()
        super().tearDown()

    def _tick(self, px: float, t: datetime) -> None:
        self.engine.record_tick(self.KEY, px, 0, t.timestamp(), None, tick_ist=t)

    def _session(self, day: date, frozen: float, close: float) -> float:
        """13:30-15:15 drifting tape, then the verified CAS shape: flat at the
        freeze value at 2 Hz until the print in the 15:28 bar, then flat.
        Returns Z as of the last continuous tick."""
        import math
        t, px = ist(day, 13, 30), frozen - 40.0
        while t < ist(day, 15, 15):
            px += 0.2 * math.sin(t.timestamp() / 7.0) + 0.004
            self._tick(round(px, 2), t)
            t += timedelta(seconds=0.5)
        z_last_cts = self.engine.symbol_states[self.KEY].z_score
        while t < ist(day, 15, 28, 30):
            self._tick(frozen, t)
            t += timedelta(seconds=0.5)
        while t < ist(day, 15, 45):
            self._tick(close, t)
            t += timedelta(seconds=0.5)
        return z_last_cts

    def test_no_signal_events_from_frozen_or_auction_prints(self):
        z_last_cts = self._session(CAS_DAY, frozen=23389.25, close=23477.80)
        cutoff = ist(CAS_DAY, 15, 15).timestamp()
        late = [e for e in self.events if e["timestamp"] >= cutoff
                and e["event_type"] in ("ALERT", "CONFIRM", "STATE_CHANGE")]
        self.assertEqual(late, [], f"signal events on frozen/auction prints: {late[:3]}")
        trend_kills = [e for e in self.events if e["timestamp"] >= cutoff
                       and str(e.get("setup_label", "")).startswith("TREND_KILL")]
        self.assertEqual(trend_kills, [], "TREND_KILL rows after the freeze would be graded off the auction print")
        st = self.engine.symbol_states[self.KEY]
        self.assertEqual(st.ltp, 23477.80, "raw tape still updates")
        self.assertIn("CAS FREEZE", st.event_blackout_reason)
        self.assertEqual(st.z_score, z_last_cts, "Z must hold its last continuous value, not ride the freeze to the cap")

    def test_open_alert_is_closed_out_at_the_freeze(self):
        st = self.engine.symbol_states[self.KEY]
        t0 = ist(CAS_DAY, 15, 14, 50)
        self._tick(23400.0, t0)
        st.sig_state, st.alert_side = 1, 1                    # an ALERT open into the close
        self._tick(23400.5, ist(CAS_DAY, 15, 15, 0.5))
        aborts = [e for e in self.events if e["event_type"] == "ABORT"]
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0]["regime"], "BLACKOUT | CAS FREEZE")
        self.assertEqual(st.sig_state, 0)

    def test_pre_cas_post_close_is_also_guarded(self):
        self._session(PRE_CAS_DAY, frozen=24430.35, close=24430.35)
        late = [e for e in self.events if e["timestamp"] >= ist(PRE_CAS_DAY, 15, 30).timestamp()
                and e["event_type"] in ("ALERT", "CONFIRM")]
        self.assertEqual(late, [])


# --------------------------------------------------------------------- chain

class TestDerivativesSpot(_TempLogs):
    """orderflow.cas.derivatives_spot: the level the derivatives complex
    trades against once the cash index freezes."""

    def setUp(self) -> None:
        super().setUp()
        from core.models import CASIndexState
        from core.state import cas_state
        import orderflow.cas as cas
        self.cas = cas
        for k in list(cas_state):
            cas_state[k] = CASIndexState()
        cas._last_sample_ts = 0.0
        cas._last_synth_ts = 0.0

    def _feed(self, day: date) -> None:
        for disp, px, (h, m, s) in (("NIFTY_FUT", 23500.0, (15, 14, 50)),
                                    ("NIFTY", 23400.0, (15, 14, 55)),
                                    ("NIFTY", 23400.0, (15, 15, 0)),
                                    ("NIFTY_FUT", 23650.0, (15, 22, 0))):
            t = ist(day, h, m, s)
            if disp == "NIFTY":
                self.cas.on_spot_tick(disp, px, t.timestamp(), t)
            else:
                self.cas.on_fut_tick(disp, px, t.timestamp(), t)

    def test_futures_implied_inside_the_cas_window_only(self):
        self._feed(CAS_DAY)
        ds = self.cas.derivatives_spot
        self.assertEqual(ds("NIFTY", 23400.0, ist(CAS_DAY, 15, 22, 5)), (23550.0, "FUT"))
        self.assertEqual(ds("NIFTY", 23400.0, ist(CAS_DAY, 15, 14)), (23400.0, "SPOT"))
        self.assertEqual(ds("NIFTY", 23477.8, ist(CAS_DAY, 15, 40)), (23477.8, "SPOT"))
        self.assertEqual(ds("BANKNIFTY", 56000.0, ist(CAS_DAY, 15, 22)), (56000.0, "FROZEN"))

    def test_nifty_snap_rows_carry_the_chain_columns(self):
        from collections import deque
        from core.state import pcr_state
        self._feed(CAS_DAY)
        t = ist(CAS_DAY, 15, 22, 5)
        saved = (dict(pcr_state.iv_history_ce), dict(pcr_state.iv_history_pe))
        try:
            pcr_state.iv_history_ce[23550] = deque([(t.timestamp() - 2, 0.1410)])
            pcr_state.iv_history_pe[23550] = deque([(t.timestamp() - 2, 0.1265)])
            self.cas.on_spot_tick("NIFTY", 23400.0, t.timestamp(), t)
        finally:
            pcr_state.iv_history_ce.clear(); pcr_state.iv_history_ce.update(saved[0])
            pcr_state.iv_history_pe.clear(); pcr_state.iv_history_pe.update(saved[1])
        with open(self._tmp / f"{CAS_DAY}_cas.csv", encoding="utf-8") as fh:
            row = [r for r in csv.DictReader(fh) if r["index"] == "NIFTY"][-1]
        self.assertEqual((row["chain_spot"], row["chain_src"], row["atm_strike"]), ("23550.00", "FUT", "23550"))
        self.assertEqual((row["iv_ce_atm"], row["iv_pe_atm"]), ("0.1410", "0.1265"))

    def test_pre_cas_day_is_always_spot(self):
        self._feed(PRE_CAS_DAY)
        self.assertEqual(self.cas.derivatives_spot("NIFTY", 23400.0, ist(PRE_CAS_DAY, 15, 22)),
                         (23400.0, "SPOT"))


class TestChainDecoupling(_TempLogs):
    """Option-chain analytics run off the live leg and outside the signal
    engine: futures-implied through the CAS window, uninterrupted by
    blackouts."""

    SPOT = "NSE_INDEX|Nifty 50"
    FUT = "NSE_FO|TESTNIFTYFUT"

    def setUp(self) -> None:
        super().setUp()
        import GammaLeak as engine
        import orderflow.gex as gex
        import orderflow.oi_flow as oi_flow
        from analytics import vol_surface
        from core.models import CASIndexState
        from core.state import cas_state, oi_flow_timeline, pcr_state
        self.engine, self.timeline, self.vol_surface = engine, oi_flow_timeline, vol_surface
        self._orig_append = engine.append_event_row
        engine.append_event_row = lambda **kw: None
        self._saved_expiry = vol_surface.get_expiry_date()
        vol_surface.set_expiry_date(None)
        engine.symbol_states.clear()
        engine.symbol_states[self.SPOT] = engine.SymbolState()
        engine.symbol_states[self.FUT] = engine.SymbolState()
        engine.DISPLAY_NAMES[self.FUT] = "NIFTY_FUT"
        for k in list(cas_state):
            cas_state[k] = CASIndexState()
        oi_flow_timeline.clear()
        oi_flow._oi_flow_last_sample_ts = 0.0
        gex._last_gex_ts = 0.0
        pcr_state.ce_ltp_by_strike.clear(); pcr_state.pe_ltp_by_strike.clear()
        # ATM straddles: 23400 (cash) and 23550 (where the futures take the chain)
        pcr_state.ce_ltp_by_strike.update({23400: 100.0, 23550: 120.0})
        pcr_state.pe_ltp_by_strike.update({23400: 95.0, 23550: 110.0})

    def tearDown(self) -> None:
        from core.state import pcr_state
        self.engine.append_event_row = self._orig_append
        self.engine.symbol_states.clear()
        self.engine.DISPLAY_NAMES.pop(self.FUT, None)
        self.vol_surface.set_expiry_date(self._saved_expiry)
        pcr_state.ce_ltp_by_strike.clear(); pcr_state.pe_ltp_by_strike.clear()
        self.timeline.clear()
        super().tearDown()

    def _tape(self, day: date, until: tuple[int, int]) -> None:
        """Cash flat at 23400 (frozen from 15:15 on CAS days); futures at a
        +100 basis until 15:15, then climbing 150 pts by 15:25."""
        t, vtt = ist(day, 14, 55), 0
        while t < ist(day, *until):
            secs = (t - ist(day, 15, 15)).total_seconds()
            fut = 23500.0 + (min(secs, 600.0) / 600.0 * 150.0 if secs > 0 else 0.0)
            vtt += 50
            self.engine.record_tick(self.FUT, fut, vtt, t.timestamp(), None, tick_ist=t)
            self.engine.record_tick(self.SPOT, 23400.0, 0, t.timestamp() + 0.1, None,
                                    tick_ist=t + timedelta(seconds=0.1))
            t += timedelta(seconds=1)

    def _samples_between(self, day: date, start: tuple[int, int], end: tuple[int, int]) -> int:
        lo, hi = ist(day, *start).timestamp(), ist(day, *end).timestamp()
        return sum(1 for s in self.timeline if lo <= s[0] < hi)

    def test_chain_follows_futures_while_cash_is_frozen(self):
        self._tape(CAS_DAY, (15, 26))
        nifty = self.engine.symbol_states[self.SPOT]
        self.assertEqual(self.engine.chain_spot(ist(CAS_DAY, 15, 26)), (23550.0, "FUT"))
        self.assertEqual(nifty.straddle_premium, 230.0, "ATM must follow the futures, not the frozen cash")
        self.assertGreater(self._samples_between(CAS_DAY, (15, 16), (15, 26)), 0,
                           "OI-flow sampling must continue through the auction")
        self.assertIn("CAS FREEZE", nifty.event_blackout_reason, "cash signals stay off")
        # The 5-min OI snapshot's price reference (OI-flow quadrant) is the
        # chain underlying too.
        from core.state import pcr_state
        saved = (pcr_state.oi_snapshot_ts, pcr_state.ltp_at_oi_snapshot)
        try:
            pcr_state.oi_snapshot_ts = None
            key = next(iter(self.engine.PCR_KEY_SIDE))
            self.engine.update_pcr_snapshot(key, 1000.0, ist(CAS_DAY, 15, 25).timestamp())
            self.assertEqual(pcr_state.ltp_at_oi_snapshot, 23550.0)
        finally:
            pcr_state.oi_snapshot_ts, pcr_state.ltp_at_oi_snapshot = saved
            pcr_state.ce_oi.clear(); pcr_state.pe_oi.clear()
            pcr_state.prev_ce_oi.clear(); pcr_state.prev_pe_oi.clear()

    def test_pre_cas_day_chain_stays_on_spot(self):
        self._tape(PRE_CAS_DAY, (15, 26))
        nifty = self.engine.symbol_states[self.SPOT]
        self.assertEqual(self.engine.chain_spot(ist(PRE_CAS_DAY, 15, 26)), (23400.0, "SPOT"))
        self.assertEqual(nifty.straddle_premium, 195.0)

    def test_blackout_silences_signals_not_the_chain(self):
        self.vol_surface.set_expiry_date(CAS_DAY)          # expiry pin blackout from 15:00
        self._tape(CAS_DAY, (15, 10))
        nifty = self.engine.symbol_states[self.SPOT]
        self.assertEqual(nifty.event_blackout_reason, "EXPIRY PIN")
        self.assertGreater(self._samples_between(CAS_DAY, (15, 0), (15, 10)), 0)


# --------------------------------------------------------------------- grader

class TestGraderTruncation(unittest.TestCase):
    def setUp(self) -> None:
        import calibrate
        self.cal = calibrate
        self._tmp = Path(tempfile.mkdtemp(prefix="gl_cal_"))
        self._old = calibrate.LOG_DIR
        calibrate.LOG_DIR = str(self._tmp)
        calibrate._price_cache.clear()

    def tearDown(self) -> None:
        self.cal.LOG_DIR = self._old
        self.cal._price_cache.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _series(self, day: date, symbol: str, points: list[tuple[datetime, float]]) -> None:
        d = self._tmp / day.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{symbol}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "symbol", "ltp"])
            for t, px in points:
                w.writerow([f"{t.timestamp():.2f}", symbol, f"{px:.2f}"])

    def _cas_day_nifty(self):
        pts, t = [], ist(CAS_DAY, 15, 0)
        while t < ist(CAS_DAY, 15, 15):
            pts.append((t, 23400.0 - 2.0 * (t - ist(CAS_DAY, 15, 0)).total_seconds() / 60))
            t += timedelta(seconds=1)
        while t < ist(CAS_DAY, 15, 28, 30):
            pts.append((t, 23370.0)); t += timedelta(seconds=1)
        while t < ist(CAS_DAY, 15, 45):
            pts.append((t, 23470.0)); t += timedelta(seconds=1)   # +100 auction print
        self._series(CAS_DAY, "NIFTY", pts)

    def test_window_stops_at_the_freeze(self):
        self._cas_day_nifty()
        fire = ist(CAS_DAY, 15, 5).timestamp()
        # side=-1 (stretched low -> fade long): only the +100 auction print
        # would make this a win; inside CTS price only fell.
        mfe, _floor, win = self.cal.grade_fire(CAS_DAY.isoformat(), "NIFTY", -1, 23390.0, fire)
        self.assertLess(mfe, 1.0)
        self.assertFalse(win)

    def test_fire_after_the_freeze_is_ungradable(self):
        self._cas_day_nifty()
        fire = ist(CAS_DAY, 15, 20).timestamp()
        self.assertIsNone(self.cal.grade_fire(CAS_DAY.isoformat(), "NIFTY", -1, 23370.0, fire))

    def test_fire_two_minutes_before_freeze_is_ungradable(self):
        self._cas_day_nifty()
        fire = ist(CAS_DAY, 15, 14).timestamp()          # < MIN_FWD_SPAN_SECS of CTS left
        self.assertIsNone(self.cal.grade_fire(CAS_DAY.isoformat(), "NIFTY", -1, 23372.0, fire))


# --------------------------------------------------------------------- resolver

class TestVenueResolution(unittest.TestCase):
    def setUp(self) -> None:
        import GammaLeak as engine
        self.engine = engine
        self._saved = engine._instrument_master_by_symbol
        today = date.today()
        wk = (today + timedelta(days=3)).isoformat()
        mo = (today + timedelta(days=20)).isoformat()
        row = lambda ex, key, exp, name: {"exchange": ex, "instrument_key": key, "expiry": exp, "name": name}
        engine._instrument_master_by_symbol = {
            "CRUDEOIL26OCTFUT": [row("NSE_COM", "NSE_COM|5", wk, "CRUDEOIL"), row("MCX_FO", "MCX_FO|6", mo, "CRUDE OIL")],
            "CRUDEOILM26OCTFUT": [row("MCX_FO", "MCX_FO|7", wk, "CRUDE OIL")],
            "SENSEX26OCTFUT": [row("BSE_FO", "BSE_FO|8", mo, "SENSEX")],
            "SENSEX5026OCTFUT": [row("BSE_FO", "BSE_FO|9", wk, "SENSEX50")],
        }

    def tearDown(self) -> None:
        self.engine._instrument_master_by_symbol = self._saved

    def _resolve(self, sym: str) -> str | None:
        return asyncio.run(self.engine.get_active_expiry_key(sym, "FUT"))[0]

    def test_crudeoil_prefers_mcx_even_when_a_twin_expires_first(self):
        self.assertEqual(self._resolve("CRUDEOIL"), "MCX_FO|6")

    def test_sensex_still_excludes_sensex50(self):
        self.assertEqual(self._resolve("SENSEX"), "BSE_FO|8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
