"""Generate a single-page recruiter-facing PDF for GammaLeak.

Run:
    python docs/generate_resume_onepager.py

Output: docs/GammaLeak_OnePager.pdf
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont  # noqa: F401 — kept for future custom-font swap


OUT = Path(__file__).resolve().parent / "GammaLeak_OnePager.pdf"

INK = HexColor("#111418")
MUTED = HexColor("#5B6068")
ACCENT = HexColor("#0B5FFF")
RULE = HexColor("#D7DAE0")
CHIP_BG = HexColor("#EEF2FF")


def draw_header(c: canvas.Canvas, w: float, h: float) -> float:
    y = h - 18 * mm
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(18 * mm, y, "Adhiraj")
    c.setFont("Helvetica", 10.5)
    c.setFillColor(MUTED)
    y -= 6 * mm
    c.drawString(
        18 * mm,
        y,
        "Data Engineering / Data Analyst / Derivatives-adjacent  ·  BITS Pilani BSc CS (online, honours)",
    )

    # contact strip (right-aligned)
    c.setFont("Helvetica", 9.5)
    c.setFillColor(INK)
    contact = [
        "adhiraj1904@gmail.com",
        "github.com/<your-handle>/GammaLeak",
    ]
    cy = h - 18 * mm
    for line in contact:
        c.drawRightString(w - 18 * mm, cy, line)
        cy -= 5 * mm

    # rule
    y -= 5 * mm
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(18 * mm, y, w - 18 * mm, y)
    return y - 7 * mm


def draw_chip(c: canvas.Canvas, x: float, y: float, text: str) -> float:
    c.setFont("Helvetica", 8.5)
    pad = 2.2 * mm
    tw = c.stringWidth(text, "Helvetica", 8.5)
    bw = tw + 2 * pad
    bh = 4.8 * mm
    c.setFillColor(CHIP_BG)
    c.setStrokeColor(CHIP_BG)
    c.roundRect(x, y - 1.4 * mm, bw, bh, 1.6 * mm, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.drawString(x + pad, y, text)
    return x + bw + 1.8 * mm


def draw_chips(c: canvas.Canvas, x0: float, y: float, x_max: float, chips: list[str]) -> float:
    x = x0
    for chip in chips:
        if x > x_max - 30 * mm:
            x = x0
            y -= 6 * mm
        x = draw_chip(c, x, y, chip)
    return y - 6 * mm


def draw_project_title(c: canvas.Canvas, x: float, y: float) -> float:
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, y, "GammaLeak — Real-time market-data streaming platform")
    c.setFont("Helvetica-Oblique", 9.5)
    c.setFillColor(MUTED)
    y -= 5 * mm
    c.drawString(
        x,
        y,
        "Solo · BITS Pilani industry project · ~8,000 LOC Python  |  Live Indian F&O tape + options microstructure",
    )
    return y - 4 * mm


def wrap_text(text: str, font: str, size: float, max_w: float, c: canvas.Canvas) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if c.stringWidth(trial, font, size) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_bullets(c: canvas.Canvas, x: float, y: float, w: float, bullets: list[str]) -> float:
    text_font = "Helvetica"
    text_size = 9.8
    bullet_indent = 4 * mm
    line_h = 4.4 * mm
    para_gap = 1.5 * mm

    for b in bullets:
        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, "▪")
        c.setFillColor(INK)
        c.setFont(text_font, text_size)
        lines = wrap_text(b, text_font, text_size, w - bullet_indent, c)
        for i, ln in enumerate(lines):
            c.drawString(x + bullet_indent, y, ln)
            y -= line_h
        y -= para_gap
    return y


def draw_metrics_strip(c: canvas.Canvas, x: float, y: float, w: float) -> float:
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(x, y, x + w, y)
    y -= 6 * mm

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(x, y, "Project metrics")
    y -= 5 * mm

    metrics = [
        ("Ticks / session", "500 k+"),
        ("Sustained rate", "50–200 ticks/s"),
        ("Instruments live", "8 + option window"),
        ("Schema cols / file", "20"),
        ("Parallel CSV writers", "8"),
        ("Broadcast", "4 Hz (≤ 250 ms)"),
        ("Flow patterns", "5 tested"),
        ("Research sessions", "45 futures days"),
    ]
    col_w = w / 4
    c.setFont("Helvetica", 9.2)
    for i, (label, value) in enumerate(metrics):
        row, col = divmod(i, 4)
        cx = x + col * col_w
        cy = y - row * 9 * mm
        c.setFillColor(MUTED)
        c.drawString(cx, cy, label)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10.2)
        c.drawString(cx, cy - 4.6 * mm, value)
        c.setFont("Helvetica", 9.2)

    rows_used = (len(metrics) + 3) // 4
    return y - rows_used * 9 * mm


def draw_footer(c: canvas.Canvas, w: float, h: float) -> None:
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 8.2)
    c.drawString(
        18 * mm,
        12 * mm,
        "Source code: github.com/<your-handle>/GammaLeak  ·  Full README + architecture in repo  ·  Not investment advice",
    )


def build() -> None:
    w, h = A4
    c = canvas.Canvas(str(OUT), pagesize=A4)
    c.setTitle("GammaLeak — One-pager")
    c.setAuthor("Adhiraj")

    y = draw_header(c, w, h)

    chips = [
        "Python 3.12",
        "asyncio",
        "FastAPI",
        "WebSocket",
        "Protocol Buffers",
        "NumPy",
        "pandas",
        "Rich",
        "OAuth2",
        "Docker",
    ]
    y = draw_chips(c, 18 * mm, y, w - 18 * mm, chips)
    y -= 1 * mm

    y = draw_project_title(c, 18 * mm, y)

    bullets = [
        "Architected an async market-data platform decoding gzip-compressed Protobuf ticks from Upstox WebSocket v3, processing 500K+ ticks per session across 8 instruments plus a dynamic NIFTY options window, with a 4 Hz FastAPI/WebSocket dashboard and per-symbol CSV streams.",
        "Built production-style data-pipeline safeguards: asyncio.Queue tick/backpressure separation, batched asyncio.to_thread disk writes, 20-column schema-aware log rotation, WebSocket silence watchdog/reconnects, and a self-healing instrument-master resolver for expiry rollovers.",
        "Implemented derivatives microstructure analytics: Lee-Ready aggressor classification, CVD, five tested flow-divergence patterns, gamma-flush detection, max-pain/gamma-wall/PCR tracking, and an OI-flow velocity chart that surfaces liquidity sweeps, floor failures, and spot-futures basis leads.",
        "Ran NIFTY/BANKNIFTY futures and macro-regime research over 45 sessions, quantifying first-hour low breaks, VWAP reclaim probability, OI change, gap behavior, and Yield/Oil/USDINR conditions; exported reproducible CSV/HTML/Excel reports for post-session analysis.",
    ]
    y = draw_bullets(c, 18 * mm, y, w - 36 * mm, bullets)

    y -= 2 * mm
    y = draw_metrics_strip(c, 18 * mm, y, w - 36 * mm)

    draw_footer(c, w, h)
    c.showPage()
    c.save()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
