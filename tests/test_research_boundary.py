"""The NSE MCP servers in .mcp.json are for research only. NSE publishes that
data for "educational and informational purposes" with a 1-3 minute delay,
so nothing in the live engine may call them.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE_PATHS = ["GammaLeak.py", "web_server.py", "core", "orderflow", "signals",
                "ui", "analytics", "gammaleak_runtime", "positioning", "static"]


class TestResearchBoundary(unittest.TestCase):
    def test_engine_never_calls_the_nse_mcp_servers(self):
        hits = []
        for rel in ENGINE_PATHS:
            p = ROOT / rel
            files = [p] if p.is_file() else [f for f in p.rglob("*") if f.suffix in (".py", ".html", ".js")]
            for f in files:
                if "mcp.nseindia" in f.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(f.relative_to(ROOT)))
        self.assertEqual(hits, [], "engine code must not use the research-only NSE MCP servers")


if __name__ == "__main__":
    unittest.main(verbosity=2)
