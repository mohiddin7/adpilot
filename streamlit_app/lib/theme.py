"""AdPilot colours, read from assets/palette.json (OKLCH-derived, WCAG-checked light values; see that file).

Status colours mean a status. Platform series use brand, forecast and audited first — never critical or open — so no
platform's line reads as an alarm. Series that are not platforms (OTHER) and severity (SEVERITY) take palette values no
platform has, so nothing but a platform ever wears a platform's colour. Colour is never the only signal (palette.json
constraints): flagged rows also carry a text label, and severity markers a shape."""

from __future__ import annotations

import json
from pathlib import Path

PALETTE = json.loads((Path(__file__).resolve().parents[1] / "assets" / "palette.json").read_text())
COLORS = {name: spec["light"] for name, spec in PALETTE["colors"].items()}
_DARK = {name: spec["dark"] for name, spec in PALETTE["colors"].items()}  # the brighter tier: 3.6:1 on the light surface
GROUNDS = PALETTE["grounds"]["light"]
SERIES = [COLORS["brand"], COLORS["forecast"], COLORS["audited"], COLORS["resolved"]]
# Warm and ordered: amber, orange, deep red. No platform has any of them.
SEVERITY = {"MODERATE": _DARK["open"], "SEVERE": _DARK["critical"], "CRITICAL": COLORS["critical"]}
# ponytail: the palette has four values that are neither a platform's nor a red, so a fifth non-platform series
# repeats the first (as SERIES did); add a palette colour when a chart needs five. The fourth is MODERATE's amber,
# which never shares a chart with severity markers.
OTHER = [COLORS["open"], _DARK["resolved"], COLORS["resolved"], _DARK["open"]]
SYMBOLS = {"MODERATE": "circle", "SEVERE": "diamond", "CRITICAL": "x"}  # colour is never the only signal
GRID = "#ebe5dc"
# The whole account as one series, when every platform is in view: ink, the app's text colour, so it is no
# platform's colour (the brand is Facebook's).
ACCOUNT = PALETTE["grounds"]["dark"]["bg"]
MUTED = "#8c8177"


def platform_colour(colors: dict[str, str], platform: str | None) -> str | None:
    """One platform's colour. A platform the pack gave no colour is neutral, never the brand (that is a platform's
    colour too). None, for the default, when there is no platform or no pack colours at all."""
    return colors.get(platform, MUTED) if platform and colors else None


def platform_colors(names: dict[str, str]) -> dict[str, str]:
    """The pack's `dashboard.colors` (palette names from /dashboard) → hex. The API only allows series colours."""
    return {platform: COLORS[name] for platform, name in names.items() if name in COLORS}
