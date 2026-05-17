# GammaLeak — Resume Bullets

> Updated after the GammaLeak rename and the newer liquidity-sweep / OI-flow / gamma-wall work.
> Pick one section based on the job description; keep 3-4 bullets on the resume.

---

## Recommended Overleaf Section

Use this as the default one-page resume version because it balances Data Engineering + derivatives analytics.

```tex
\resumeProjectHeading
  {\textbf{GammaLeak} $|$ \emph{Python, FastAPI, asyncio, WebSockets, Protobuf, Pandas}}{Jan 2026 -- Present}
  \resumeItemListStart
    \resumeItem{Architected an async market-data platform decoding gzip-compressed Protobuf ticks from Upstox WebSocket v3, processing 500K+ ticks per session across 8 instruments plus a dynamic NIFTY options window, with a 4 Hz FastAPI/WebSocket dashboard and per-symbol CSV streams.}
    \resumeItem{Built production-style data-pipeline safeguards: \texttt{asyncio.Queue} tick/backpressure separation, batched \texttt{asyncio.to\_thread} disk writes, 20-column schema-aware log rotation, WebSocket silence watchdog/reconnects, and a self-healing instrument-master resolver for expiry rollovers.}
    \resumeItem{Implemented derivatives microstructure analytics: Lee-Ready aggressor classification, CVD, five tested flow-divergence patterns, gamma-flush detection, max-pain/gamma-wall/PCR tracking, and an OI-flow velocity chart that surfaces liquidity sweeps, floor failures, and spot-futures basis leads.}
    \resumeItem{Ran NIFTY/BANKNIFTY futures and macro-regime research over 45 sessions, quantifying first-hour low breaks, VWAP reclaim probability, OI change, gap behavior, and Yield/Oil/USDINR conditions; exported reproducible CSV/HTML/Excel reports for post-session analysis.}
  \resumeItemListEnd
```

Shorter 3-bullet version if space gets tight:

```tex
\resumeProjectHeading
  {\textbf{GammaLeak} $|$ \emph{Python, FastAPI, asyncio, WebSockets, Protobuf, Pandas}}{Jan 2026 -- Present}
  \resumeItemListStart
    \resumeItem{Built an async market-data pipeline decoding gzip-compressed Protobuf ticks from Upstox WebSocket v3, processing 500K+ ticks/session across 8 instruments plus live NIFTY options data, and broadcasting a 4 Hz FastAPI/WebSocket dashboard.}
    \resumeItem{Engineered reliability layers including \texttt{asyncio.Queue} backpressure, batched threaded CSV writes, 20-column schema-aware log rotation, WebSocket watchdog reconnects, and dynamic expiry resolution from the Upstox instrument master.}
    \resumeItem{Added derivatives analytics for liquidity sweeps and flow confirmation using Lee-Ready CVD, tested divergence detectors, OI-flow velocity, max-pain/gamma walls, PCR, gamma-flush detection, and spot-futures basis overlays.}
  \resumeItemListEnd
```

---

## A. Data Engineering / Streaming Platforms

> Best for DE, market-data engineering, backend, fintech infra, and GCC engineering roles.

**GammaLeak — Real-time market-data streaming platform** · Python · asyncio · FastAPI · Protocol Buffers · WebSocket v3 · *2026*

- Built a low-latency ingestion pipeline for gzip-compressed Protobuf ticks from a live Upstox WebSocket feed, processing 500K+ ticks per session across 8 instruments plus a dynamic NIFTY options window.
- Decoupled feed parsing, mathematical computation, WebSocket fan-out, and disk persistence with `asyncio.Queue`, queue backpressure handling, and batched `asyncio.to_thread` CSV writes.
- Designed a 20-column versioned tick schema with per-symbol CSV streams and schema-aware rotation that archives stale files automatically before opening fresh logs.
- Hardened live-market failure paths with a WebSocket silence watchdog, reconnect loop, depth-drop-safe order-book extraction, OAuth refresh, and a self-healing instrument-master resolver for monthly expiry rollovers.
- Exposed the engine through a FastAPI/WebSocket backend broadcasting JSON dashboard state at 4 Hz to a vanilla-JS frontend.

---

## B. Data Analyst / Equity Research / Derivatives Research

> Best for data analyst, equity research analyst, derivatives research, market analytics, and quant-adjacent roles.

**GammaLeak — Intraday derivatives analytics and research suite** · Python · pandas · NumPy · FastAPI · *2026*

- Built a real-time F&O analytics engine computing VWAP, rolling σ, Z-score, Kaufman Efficiency Ratio, Hurst exponent, ATR, gap buckets, OI flow, CVD, and plain-English verdict tiers across Indian index futures and key cross-asset drivers.
- Implemented liquidity-sweep diagnostics using live PE/CE OI delta velocity, max-pain and gamma-wall anchors, NIFTY spot/FUT basis overlays, and floor-failure / max-pain-magnet scenarios rendered in the dashboard.
- Modelled order-flow confirmation with Lee-Ready aggressor classification, cumulative volume delta, and five tested divergence patterns: buyer exhaustion, seller exhaustion, breakout confirmation, buy absorption, and sell absorption.
- Ran a 45-session NIFTY/BANKNIFTY futures regime study over Feb-May 2026, measuring first-hour low breaks, VWAP reclaim probability, OI change, gaps, and macro tags including Yield, Oil, and USDINR.
- Produced reproducible research artifacts across CSV, Markdown, HTML, Excel, and live tick logs for post-session review and hypothesis testing.

---

## C. Derivatives / Risk-Tech / Trade-Lifecycle With Engineering

> Best for derivatives ops, risk-tech, trade lifecycle, and market-data engineering.

**GammaLeak — Live F&O microstructure radar** · Python · WebSockets · FastAPI · pandas · *2026*

- Built and ran a live monitor for Indian index F&O that ingests NIFTY, BANKNIFTY, front-month futures, VIX, USDINR, crude, RELIANCE, HDFCBANK, and NIFTY option-chain state.
- Added F&O-specific primitives: dynamic expiry rollover, ATM strike-window tracking, PCR, ATM straddle box, OI rate-of-change, full-chain max-pain polling, near-ATM walls, and deep OI cluster detection.
- Detected actionable microstructure states including liquidity sweeps, PE/CE wall failures, gamma flushes, spot-futures basis leads, CVD divergences, and flow-confirmed opening-range breakouts.
- Mirrored futures-derived order-flow verdicts onto spot-index cards so volume-less spot indices inherit the active futures leg's CVD and flow context.

---

## Interview Talking Points

| Question | High-signal answer |
|---|---|
| What is GammaLeak? | A live market-data streaming and derivatives analytics platform: Protobuf WebSocket ingestion, async processing, per-symbol persistence, and a 4 Hz dashboard for Indian F&O microstructure. |
| What is the data-engineering angle? | It handles live feed decoding, queue backpressure, schema evolution, per-symbol append streams, reconnect/watchdog logic, dynamic instrument resolution, and API-driven enrichment. |
| What is the research angle? | It turns tick/OI/order-book data into testable features: VWAP reclaim, first-hour breakdowns, CVD divergence, OI-flow velocity, gamma walls, max pain, and macro-regime overlays. |
| What was hard? | Avoiding silent failure in live feeds: schema drift, stale expiry contracts, exchange depth drops, queue spikes, and false exhaustion fires on gap-day opens. |
| What changed with liquidity sweeps? | The dashboard now plots spot/FUT against PE/CE walls and max pain while also plotting PE/CE OI delta velocity, so sweeps, floor failures, and basis-led breakdowns become visually inspectable. |

---

## Project Metrics Worth Quoting

| Metric | Value |
|---|---|
| Production Python LOC | ~8,000+ across engine, core, orderflow, signals, analytics, UI serializers, and FastAPI backend |
| Main engine | `GammaLeak.py` (~3,700 lines) |
| Dashboard frontend | `static/index.html` (~1,700 lines) |
| Live instruments | 8 core instruments + dynamic NIFTY option strikes |
| Live throughput | 500K+ ticks/session; 50-200 ticks/s sustained; 200+ ticks/s bursts |
| Broadcast cadence | 4 Hz FastAPI/WebSocket dashboard |
| CSV schema | 20 columns, per-symbol files, schema-aware rotation |
| Tested flow patterns | 5 CVD divergence patterns + depth-drop and gap-day false-fire tests |
| Research coverage | 45 NIFTY/BANKNIFTY futures sessions, macro-regime overlays, CSV/HTML/Excel outputs |
