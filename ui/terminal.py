"""Rich Live TUI dashboard renders.

Composes the in-terminal dashboard that `GammaLeak.py` displays when
run standalone (without the web frontend). Every function here is a pure
read of engine state → Rich Panel/Group/Text. No mutation, no side effects.

The engine module is bound as `engine` and accessed via attribute lookup at
CALL time, not import time — this keeps the circular dependency between
GammaLeak (which imports build_dashboard) and ui.terminal (which
reads engine state) safe: ui.terminal can load against a partial engine
module because no attribute access happens at module load.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import GammaLeak as engine
from core.models import SymbolState


def format_macro_header() -> str:
    now_ist = engine.get_reference_time_ist()
    bias = engine.compute_desk_bias()
    session_line = (
        f"Session: {engine.get_session_badge(now_ist)}  |  "
        f"Desk Bias: {bias['label']} ({float(bias['score']):+.2f})"
    )
    quotes_line = "  |  ".join(
        [
            str(bias["vix_text"]),
            str(bias["usdinr_text"]),
            str(bias["pcr_text"]),
        ]
    )
    # V5.0: Append FII/DII positioning if available
    fii_line = ""
    if engine._fii_snapshot and engine._fii_snapshot.fii:
        fii_line = f"\n{engine._fii_snapshot.format_summary()}"

    # V5.0: Append active Sonar catalyst alerts
    sonar_line = ""
    if engine._sonar_last_contexts:
        active = [ctx for ctx in engine._sonar_last_contexts.values() if ctx.has_catalyst]
        if active:
            tags = [ctx.format_short() for ctx in active]
            sonar_line = f"\nSONAR: {' | '.join(tags)}"

    return f"{session_line}\n{quotes_line}{fii_line}{sonar_line}"


def focus_sort_key(item: tuple[int, float, int, int]) -> tuple[int, float, int, int]:
    state_rank, score, priority, abs_z_rank = item
    return (-state_rank, -score, priority, -abs_z_rank)


def build_focus_panel() -> Panel:
    rows: list[tuple[tuple[int, float, int, int], str]] = []

    for key in engine.INSTRUMENT_KEYS[:engine.TOP_N_ROWS]:
        state = engine.symbol_states[key]
        if state.action_signal == engine.SIGNAL_TREND_STAND_DOWN:
            continue

        raw_focus_score = abs(state.z_score) * state.efficiency_ratio
        if state.efficiency_ratio < engine.ER_TREND_THRESHOLD or state.hurst < engine.HURST_THRESHOLD:
            score = 0.0
            signal_status = "STAND DOWN - CHOP"
        else:
            score = raw_focus_score
            signal_status = "READY"
        if state.regime_shift_alert or state.thesis_decay:
            state_rank = 3
        elif state.sig_state == 2:
            state_rank = 2
        elif state.sig_state == 1:
            state_rank = 1
        else:
            state_rank = 0
        if state_rank == 0 and abs(state.z_score) < 1.0:
            continue

        tags: list[str] = [state.action_signal.split(" | ")[0], f"Z {state.z_score:+.2f}"]
        if engine.is_outside_opening_range(state):
            tags.append("OR Break")
        elif state.is_fakeout:
            tags.append("Fakeout")
        if state.regime_shift_alert:
            tags.append("Shift")
        elif state.thesis_decay:
            tags.append("Decay")
        if signal_status == "STAND DOWN - CHOP":
            tags.append(signal_status)

        label = (
            f"{engine.get_display_name(key)}  |  Score {score:.2f}  |  "
            + "  |  ".join(tags)
        )
        sort_key = (
            state_rank,
            score,
            engine.FOCUS_PRIORITY.get(key, 999),
            int(abs(state.z_score) * 100),
        )
        rows.append((sort_key, label))

    rows.sort(key=lambda item: focus_sort_key(item[0]))
    if not rows:
        body = "No active focus names."
    else:
        body = "\n".join(
            f"{index}. {label}" for index, (_, label) in enumerate(rows[:engine.FOCUS_TOP_N], start=1)
        )
    return Panel(body, title="Focus", border_style="magenta")


def format_thesis_readout(state: SymbolState) -> Text:
    if state.sig_state != 2 or state.thesis_started_at is None:
        return Text("-", style="dim white")

    total_seconds = int(state.thesis_age_secs)
    minutes, seconds = divmod(total_seconds, 60)

    if minutes < 10:
        label = f"{minutes:02d}m{seconds:02d}s"
        style = "yellow"
    elif minutes < 15:
        label = f"AGING {minutes:02d}m"
        style = "dark_orange"
    elif minutes < 20:
        label = f"STALE {minutes:02d}m"
        style = "bold red"
    else:
        label = f"KILLED {minutes:02d}m"
        style = "dim white"

    if state.regime_shift_alert:
        label = f"SHIFT {minutes:02d}m"
        style = "bold red"

    return Text(label, style=style)


def format_pcr_footer(log_path: Path) -> str:
    nifty_state = engine.symbol_states.get("NSE_INDEX|Nifty 50")
    nifty_ltp = nifty_state.ltp if nifty_state else 0.0
    ratio, ce_sum, pe_sum, window_strikes, atm_strike = (
        engine.pcr_state.get_dynamic_snapshot(nifty_ltp) if nifty_ltp > 0 else (None, 0.0, 0.0, (), None)
    )

    if ratio is None or atm_strike is None:
        pcr_text = "PCR(ATM±100): awaiting NIFTY/OI"
    else:
        prev_ce_sum = sum(engine.pcr_state.prev_ce_oi.get(s, 0.0) for s in window_strikes)
        prev_pe_sum = sum(engine.pcr_state.prev_pe_oi.get(s, 0.0) for s in window_strikes)

        delta_ce = ce_sum - prev_ce_sum if engine.pcr_state.oi_snapshot_ts else 0.0
        delta_pe = pe_sum - prev_pe_sum if engine.pcr_state.oi_snapshot_ts else 0.0

        ce_arrow = "↑" if delta_ce > 0 else "↓" if delta_ce < 0 else ""
        pe_arrow = "↑" if delta_pe > 0 else "↓" if delta_pe < 0 else ""

        strike_text = ", ".join(str(strike) for strike in window_strikes)
        pcr_text = (
            f"PCR ATM {atm_strike}: {ratio:.2f}  |  "
            f"PE OI: {pe_sum:,.0f} ({pe_arrow}{abs(delta_pe):,.0f})  |  "
            f"CE OI: {ce_sum:,.0f} ({ce_arrow}{abs(delta_ce):,.0f})  |  Strikes: {strike_text}"
        )
    # V3.0: TPS and Gamma Flush status
    nifty_st = engine.symbol_states.get("NSE_INDEX|Nifty 50")
    tps_text = f"TPS: {int(nifty_st.tps)}" if nifty_st else "TPS: -"
    if nifty_st and nifty_st.gamma_flush_active:
        gf_dir = "LONG" if nifty_st.gamma_flush_side > 0 else "SHORT"
        gf_text = f"GF: {gf_dir}"
    else:
        gf_text = "GF: -"
    return f"{pcr_text}  |  {tps_text}  |  {gf_text}  |  CSV: {log_path.as_posix()}"


def format_v4_footer() -> str:
    """V4.0: Format the adaptive regime panel showing straddle box, ATR, OI RoC."""
    nifty = engine.symbol_states.get("NSE_INDEX|Nifty 50")
    if nifty is None or nifty.ltp <= 0:
        return "V4 Adaptive: Awaiting data"

    # Straddle box
    if nifty.implied_upper > 0 and nifty.implied_lower > 0:
        box_str = f"Box: {nifty.implied_lower:,.0f}-{nifty.implied_upper:,.0f} (Straddle: {nifty.straddle_premium:.0f})"
    else:
        box_str = "Box: N/A"

    # ATR
    atr_str = f"ATR: {nifty.atr:.1f}" if nifty.atr > 0 else "ATR: warmup"
    ratio_str = f"x{nifty.atr_ratio:.2f}" if nifty.atr > 0 else ""

    # OI RoC
    ce_roc_str = f"CE:{nifty.oi_roc_ce_atm:+.1f}%"
    pe_roc_str = f"PE:{nifty.oi_roc_pe_atm:+.1f}%"

    # Dynamic regime
    regime_str = nifty.dynamic_regime if nifty.dynamic_regime else "---"

    # Anchor gate
    anchor_str = nifty.anchor_gate if nifty.anchor_gate else "---"

    # V5.1: OI flow classification
    flow_str = nifty.oi_flow_label if nifty.oi_flow_label else "---"
    cepe_str = nifty.oi_flow_ce_pe if nifty.oi_flow_ce_pe else "---"

    return f"  {box_str}  |  {atr_str} {ratio_str}  |  OI RoC [{ce_roc_str} {pe_roc_str}]  |  Flow: {flow_str}  |  {cepe_str}  |  Regime: {regime_str}  |  Gate: {anchor_str}"


def build_index_driver_panel() -> Panel:
    lines: list[str] = []
    if not engine.index_driver_state.metrics:
        return Panel("Index Drivers: warmup", title="Index Drivers", border_style="magenta")
    for m in engine.index_driver_state.metrics:
        name_a = engine.get_display_name(m.pair[0])
        name_b = engine.get_display_name(m.pair[1])
        if m.stale:
            lines.append(f"  {name_a} <> {name_b}: warmup")
            continue
        corr_color = (
            "bright_green" if abs(m.corr) >= 0.7
            else "yellow" if abs(m.corr) >= 0.4
            else "red"
        )
        if m.lead_lag_secs > 0:
            lag_str = f"{name_a} leads +{m.lead_lag_secs}s"
        elif m.lead_lag_secs < 0:
            lag_str = f"{name_b} leads +{-m.lead_lag_secs}s"
        else:
            lag_str = "coincident"
        drag_color = {"DRAG": "bright_red", "DIVERGE": "bright_yellow", "BOOST": "bright_green"}.get(m.drag, "white")
        drag_tag = f"  [{drag_color}]{m.drag}[/]" if m.drag else ""
        lines.append(
            f"  {name_a} <> {name_b}: "
            f"p=[{corr_color}]{m.corr:+.2f}[/]  "
            f"{lag_str} (r={m.lead_corr:+.2f}){drag_tag}"
        )
        if m.drag:
            lines.append(f"      {m.drag_detail}")
    import time as _time
    age = int(_time.time() - engine.index_driver_state.last_refresh_ts) if engine.index_driver_state.last_refresh_ts else -1
    title = f"Index Drivers (5m window, updated {age}s ago)"
    return Panel("\n".join(lines), title=title, border_style="magenta")


def build_oi_levels_panel() -> Panel:
    """Rich panel showing NIFTY max-pain + top CE/PE walls."""
    lvl = engine.oi_levels_state.levels.get("NIFTY")
    if lvl is None:
        return Panel("[dim]OI levels initialising...[/dim]", title="OI Levels", border_style="dim")

    lines: list[str] = []
    if lvl.stale:
        hdr = f"[dim]NIFTY[/dim]  [yellow]stale[/yellow] — {lvl.stale_reason}"
        if lvl.spot > 0:
            hdr += f"   [dim]spot ₹{lvl.spot:,.1f}[/dim]"
        lines.append(hdr)
    else:
        pull = "↑" if lvl.max_pain > lvl.spot else ("↓" if lvl.max_pain < lvl.spot else "→")
        lines.append(
            f"[bold]NIFTY[/bold]  spot [cyan]₹{lvl.spot:,.1f}[/cyan]   "
            f"Max-Pain [yellow]{lvl.max_pain:,}[/yellow] "
            f"({lvl.max_pain_dist_pct:+.2f}% {pull})   "
            f"[dim]{lvl.n_strikes} strikes · {lvl.expiry}[/dim]"
        )
        if lvl.ce_walls:
            parts = [
                f"[red]R{i+1} {w.strike:,}[/red] [dim]({w.dist_pct:+.2f}%)[/dim] {w.oi/1e5:.1f}L"
                for i, w in enumerate(lvl.ce_walls)
            ]
            lines.append("  [red]Resistance (CE walls):[/red] " + "   ".join(parts))
        else:
            lines.append("  [red]Resistance:[/red] [dim]no CE OI in band[/dim]")
        if lvl.pe_walls:
            parts = [
                f"[green]S{i+1} {w.strike:,}[/green] [dim]({w.dist_pct:+.2f}%)[/dim] {w.oi/1e5:.1f}L"
                for i, w in enumerate(lvl.pe_walls)
            ]
            lines.append("  [green]Support (PE walls):[/green]    " + "   ".join(parts))
        else:
            lines.append("  [green]Support:[/green]    [dim]no PE OI in band[/dim]")

    import time as _time
    age = int(_time.time() - engine.oi_levels_state.last_refresh_ts) if engine.oi_levels_state.last_refresh_ts else -1
    title = f"OI Levels (Max-Pain + Gamma Walls, updated {age}s ago)"
    return Panel("\n".join(lines), title=title, border_style="yellow", padding=(0, 1))


def build_pre_open_panel() -> "Panel":
    """Summary panel of pre-open gap state for the indices + futures."""
    rows: list[str] = []
    watch = [
        ("NSE_INDEX|Nifty 50",  "NIFTY"),
        ("NSE_INDEX|Nifty Bank","BANKNIFTY"),
    ]
    # Also add NIFTY_FUT / BN_FUT if their resolved keys are in symbol_states
    for k, st in engine.symbol_states.items():
        if engine.DISPLAY_NAMES.get(k) in ("NIFTY_FUT", "BN_FUT"):
            watch.append((k, engine.DISPLAY_NAMES[k]))

    for key, label in watch:
        st = engine.symbol_states.get(key)
        if st is None or st.prior_close <= 0:
            rows.append(f"  {label:10s}  prior_close: [dim]n/a[/dim]")
            continue
        if st.gap_pct == 0 and st.gap_bucket == "":
            rows.append(f"  {label:10s}  prior={st.prior_close:,.2f}   [dim](waiting for first tick)[/dim]")
            continue
        col = "white"
        if st.gap_bucket in ("LARGE_GAP_UP", "SMALL_GAP_UP"):  col = "green"
        elif st.gap_bucket in ("LARGE_GAP_DN", "SMALL_GAP_DN"):  col = "red"
        sign = "+" if st.gap_pct >= 0 else ""
        rows.append(
            f"  {label:10s}  prior={st.prior_close:,.2f}  open→{st.ltp:,.2f}  "
            f"[{col}]{sign}{st.gap_pct:.2f}%  ({st.gap_bucket})[/{col}]"
        )
    body = "\n".join(rows) if rows else "  (no symbols seeded)"
    return Panel(body, title="Pre-Open Context", border_style="magenta")


def _verdict_style(confidence: str) -> str:
    if confidence == "HIGH":  return "bold white on dark_red"
    if confidence == "MED":   return "bold yellow"
    return "dim white"


def build_dashboard(log_path: Path) -> Group:
    import time as _time
    table = Table(title="GammaLeak - Adaptive VWAP + Plain-English Layer", show_header=True, header_style="bold cyan", border_style="blue", expand=False)
    for c in ["Symbol", "LTP", "VWAP", "Z", "ER", "Hurst", "Book", "Gap", "CVD", "Verdict (why)", "Action Signal"]:
        table.add_column(c, justify="right" if c in ["LTP", "VWAP", "Z", "ER", "Hurst", "Book", "Gap", "CVD"] else "left")

    now_ist = datetime.now(engine.IST)
    past_warmup = (now_ist.hour, now_ist.minute) >= (engine.WARMUP_HOUR, engine.WARMUP_MINUTE)

    visible_keys = [k for k in engine.INSTRUMENT_KEYS if k not in engine.HIDDEN_FROM_CARDS][:engine.TOP_N_ROWS]
    for key in visible_keys:
        s = engine.symbol_states[key]
        display_signal = s.action_signal
        display_style = s.action_style
        # Fix: if wall clock is past warmup but no ticks arrived, show real status
        if past_warmup and display_signal == engine.SIGNAL_WARMING_UP:
            if s.ltp <= 0:
                display_signal = "NO DATA"
                display_style = "bold yellow"
            else:
                display_signal = engine.SIGNAL_NO_EDGE
                display_style = "dim white"
        # V5.0: Annotate signal with Sonar catalyst tag if present
        sonar_ctx = engine._sonar_last_contexts.get(engine.get_display_name(key))
        if sonar_ctx and sonar_ctx.has_catalyst:
            display_signal = f"{display_signal} *{sonar_ctx.catalyst_type}*"

        # V5.2: amber pre-alert chip (display-only; does not change sig_state)
        if s.amber_active and s.sig_state < 1:
            arrow = "↑" if s.amber_side > 0 else "↓"
            tag = f"[AMBER {arrow}"
            if s.amber_reason == "DRIVER" and engine.index_driver_state.nifty_driver_amber_source:
                tag += f" {engine.index_driver_state.nifty_driver_amber_source}"
            elif s.amber_reason:
                tag += f" {s.amber_reason}"
            tag += "]"
            display_signal = f"{tag} {display_signal}"
            display_style = "bold yellow"
        # V5.2: tick-rate spike marker
        if s.tick_rate_spike:
            display_signal = f"⚡ {display_signal}"

        # μZ cell — dim when warming, green/red when stretched
        if s.micro_std_dev > 0:
            mz_style = "bold red" if s.micro_z_score >= 2 else (
                "bold green" if s.micro_z_score <= -2 else (
                    "yellow" if abs(s.micro_z_score) >= 1 else "white"
                )
            )
            mz_text = Text(f"{s.micro_z_score:+.2f}", style=mz_style)
        else:
            mz_text = Text("--", style="dim")

        # Book imbalance cell — green if buyers, red if sellers, dim if flat / unavailable
        if abs(s.book_imbalance) >= 0.05:
            book_style = "bold green" if s.book_imbalance > 0 else "bold red"
            book_text = Text(f"{s.book_imbalance:+.2f}", style=book_style)
        else:
            book_text = Text("--", style="dim")

        # Gap cell — colored by bucket
        if s.gap_bucket:
            gap_style = "bold green" if "GAP_UP" in s.gap_bucket else (
                "bold red" if "GAP_DN" in s.gap_bucket else "dim")
            gap_text = Text(f"{s.gap_pct:+.2f}%", style=gap_style)
        else:
            gap_text = Text("--", style="dim")

        # Plain-English verdict cell — bright for HIGH conviction, dim for LOW.
        # For spot indices (NIFTY, BANKNIFTY) the verdict is mirrored from the
        # corresponding front-month FUT since spot has no order-flow data of its own.
        eff_verdict, eff_why, eff_conf = engine.get_effective_verdict(key)
        verdict_display = eff_verdict or "—"
        why_display = f"  [dim]({eff_why})[/dim]" if eff_why else ""
        verdict_text = Text.from_markup(f"[{_verdict_style(eff_conf)}]{verdict_display}[/]{why_display}")

        # CVD cell — color by sign, dim when zero (spot indices, options, warmup)
        if s.cvd != 0:
            cvd_style = "bold green" if s.cvd > 0 else "bold red"
            cvd_text = Text(f"{s.cvd:+,d}", style=cvd_style)
        else:
            cvd_text = Text("--", style="dim")

        table.add_row(
            engine.get_display_name(key),
            Text(f"{s.ltp:,.2f}", style=s.ltp_style),
            f"{s.vwap:,.2f}",
            f"{s.z_score:+.2f}",
            f"{s.efficiency_ratio:.2f}",
            f"{s.hurst:.2f}",
            book_text,
            gap_text,
            cvd_text,
            verdict_text,
            Text(display_signal, style=display_style),
        )

    now_ts = _time.time()
    engine.refresh_index_driver_metrics(now_ts)
    engine.refresh_oi_levels(now_ts)

    return Group(
        Panel(format_macro_header(), title="Macro", border_style="cyan"),
        build_pre_open_panel(),
        build_focus_panel(),
        table,
        build_index_driver_panel(),
        build_oi_levels_panel(),
        Panel(format_v4_footer(), title="V4 Adaptive Engine", border_style="yellow"),
        Panel(format_pcr_footer(log_path), title="Desk Footer", border_style="green"),
    )
