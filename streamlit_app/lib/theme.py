"""AdPilot colours, read from assets/palette.json (OKLCH-derived, WCAG-checked light values; see that file).

Status colours mean a status. Chart series use brand, forecast and audited first — never critical or open — so no
platform's line reads as an alarm. Colour is never the only signal (palette.json constraints): flagged rows also carry
a text label."""

from __future__ import annotations

import json
from pathlib import Path

PALETTE = json.loads((Path(__file__).resolve().parents[1] / "assets" / "palette.json").read_text())
COLORS = {name: spec["light"] for name, spec in PALETTE["colors"].items()}
GROUNDS = PALETTE["grounds"]["light"]
SERIES = [COLORS["brand"], COLORS["forecast"], COLORS["audited"], COLORS["resolved"]]
