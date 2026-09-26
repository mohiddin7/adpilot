"""Markdown for the GitHub issue. Structure is fixed; every dynamic string is escaped here, in one place, because
campaign names are data and the issue is public."""

from __future__ import annotations

import re
from datetime import date

from adpilot.brief.analyses import Item, day, money

MAX_CHARS = 60_000  # GitHub rejects an issue body over 65 536 characters


def esc(s: str) -> str:
    """Markdown-inert text: no emphasis, links, headings, HTML, tables, mentions or autolinks."""
    s = re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", str(s))
    return s.replace("@", "@​").replace("://", ":​//")


def _table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    cols = list(rows[0])
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols),
            *("| " + " | ".join(esc(r.get(c, "")) for c in cols) + " |" for r in rows), ""]


def chart(series: dict) -> list[str]:
    pts = series["points"]
    labels = ", ".join(f'"{d:%m-%d}"' for d, _ in pts)
    values = ", ".join(f"{v:.2f}" for _, v in pts)
    top = max(v for _, v in pts) * 1.2
    return ["```mermaid", "xychart-beta",
            f'    title "{esc(series["platform"])} cost per sale: actual to {series["split"]}, then forecast"',
            f"    x-axis [{labels}]", f'    y-axis "USD" 0 --> {top:.0f}', f"    line [{values}]", "```", ""]


def render(*, as_of: date, latest: date, items: list[Item], text: dict, ahead: list[str], series: dict | None,
           followed: list[str], fine: list[str], warnings: list[str], footer: str, numbers: bool = True) -> str:
    out = [f"# Daily brief · week ending {as_of:%a} {day(as_of)} {as_of.year}", "", f"**{esc(text['headline'])}**", ""]
    if text.get("story"):
        out += [esc(text["story"]), ""]
    for n, i in enumerate(items, 1):
        t = text[i.id]
        out += [f"## {n}. {esc(t['title'])} (about {esc(money(i.stake))}{' ' + i.per if i.per else ''} at stake)",
                f"- **What happened:** {esc(i.happened)}",
                f"- **What we checked:** {esc(t['checked'])}",
                f"- **Do:** {esc(t['do'])}",
                f"- **Confidence:** {esc(i.confidence)}",
                f"- **We'll check:** {esc(i.check_line)}, in the next brief", ""]
    if ahead or series:
        out += ["## Looking ahead (next 14 days, if nothing changes)", *(f"- {esc(a)}" for a in ahead), ""]
        if series and len(series["points"]) >= 2:
            out += chart(series)
    if followed:
        out += ["## Following up", *(f"- {esc(f)}" for f in followed), ""]
    out += ["## Checked", *(f"- ⚠ {esc(w)}" for w in warnings), *(f"- {esc(f)}" for f in fine)]
    if not (warnings or fine):
        out.append("- Nothing else to report.")
    out.append("")
    if numbers and items:
        out += ["<details><summary>The numbers behind this brief</summary>", ""]
        for n, i in enumerate(items, 1):
            out += [f"**{n}. {esc(i.subject)}**", "", *_table(i.numbers)]
        out += ["</details>", ""]
    out += ["---", footer + f" · data complete through {day(as_of)}, spend through {day(latest)}", ""]
    md = "\n".join(out)
    if len(md) > MAX_CHARS and numbers:
        return render(as_of=as_of, latest=latest, items=items, text=text, ahead=ahead, series=series,
                      followed=followed, fine=fine, warnings=warnings, footer=footer, numbers=False)
    return md[:MAX_CHARS]

