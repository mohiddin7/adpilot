"""Pure view logic for the dashboard pages: date presets, widget state → API query pairs, KPI deltas, the chat context
line. No Streamlit import, so every rule a page relies on has a plain unit test (tests/test_dashboard_controls.py)."""

from __future__ import annotations

import re
from datetime import date, timedelta

MAX_RANGE_DAYS = 366  # the API's cap, adpilot/dashboard/filters.py
QUESTION_LIMIT = 600  # sanitize_question's cap inside ask()
PRESETS = ("Last 7 days", "Last 14 days", "Last 30 days", "Last 90 days", "Month to date", "Last 12 months", "Custom")
_DAYS = {"Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30, "Last 90 days": 90, "Last 12 months": 365}
LOWER_IS_BETTER = {"cpa"}


def preset_range(preset: str, date_min: date, date_max: date) -> tuple[date, date]:
    """Presets count back from the newest day in the data, not from today, so a warehouse that loads yesterday's data
    (or the bundled demo month) never opens on an empty "last 7 days"."""
    if preset in _DAYS:
        start = date_max - timedelta(days=_DAYS[preset] - 1)
    elif preset == "Month to date":
        start = date_max.replace(day=1)
    else:
        raise ValueError(f"no fixed range for {preset!r}")
    return max(start, date_min), date_max


def clamp_range(start: date, end: date) -> tuple[date, date, bool]:
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return end - timedelta(days=MAX_RANGE_DAYS - 1), end, True
    return start, end, False


def prior_range(start: date, end: date) -> tuple[date, date]:
    """The same number of days immediately before: what a ▲/▼ delta compares against."""
    prior_end = start - timedelta(days=1)
    return prior_end - (end - start), prior_end


def with_dates(params: list[tuple[str, str]], start: date, end: date) -> list[tuple[str, str]]:
    """The same filters over another date range (the comparison period)."""
    rest = [(k, v) for k, v in params if k not in ("date_from", "date_to")]
    return [("date_from", start.isoformat()), ("date_to", end.isoformat()), *rest]


def to_params(start: date, end: date, selected: dict[str, list[str]],
              ranges: dict[str, tuple[float, float]] | None = None,
              bounds: dict[str, tuple[float, float]] | None = None) -> list[tuple[str, str]]:
    """Widget state → the API's query pairs. A range still at its full bounds is left out: sending it would drop rows
    where the column is NULL (cost per acquisition on a day with no sales) without the viewer asking."""
    params = [("date_from", start.isoformat()), ("date_to", end.isoformat())]
    for column, values in selected.items():
        params += [(column, v) for v in values]
    for column, (lo, hi) in (ranges or {}).items():
        low, high = (bounds or {}).get(column, (lo, hi))
        if lo > low:
            params.append((f"{column}_min", repr(float(lo))))
        if hi < high:
            params.append((f"{column}_max", repr(float(hi))))
    return params


def keep_valid(selected: list[str], options: list[str]) -> list[str]:
    """Drop choices an upstream change made impossible; st.multiselect raises on a value that is not an option."""
    allowed = set(options)
    return [v for v in selected if v in allowed]


def clamp_pair(pair: tuple[float, float], lo: float, hi: float) -> tuple[float, float]:
    """A remembered slider position squeezed into new bounds; st.slider raises on a value outside them."""
    a, b = (min(max(x, lo), hi) for x in pair)
    return (a, b) if a <= b else (lo, hi)


def applies_to(platforms: list[str] | None, chosen: list[str]) -> bool:
    """The API's panel_applies rule: platform-specific only when every chosen platform is one of them."""
    return not platforms or (bool(chosen) and set(chosen) <= set(platforms))


def delta_pct(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior) * 100


def context_line(page: str, start: date, end: date, selected: dict[str, list[str]], limit: int = 200) -> str:
    """What the viewer is looking at, so "why did this drop?" asked from the sidebar has a subject."""
    parts = [f"{page} page", f"{start.isoformat()} to {end.isoformat()}"]
    for column, values in selected.items():
        if values:
            shown = ", ".join(values[:3]) + (f" and {len(values) - 3} more" if len(values) > 3 else "")
            parts.append(f"{column.replace('_', ' ')}: {shown}")
    line = "; ".join(parts)
    return line if len(line) <= limit else line[: limit - 3] + "..."


def with_context(question: str, context: str) -> str:
    """Prefix the context unless that would push the question past the analyst's 600-character limit — then send the
    question alone rather than have it refused."""
    q = question.strip()
    full = f"[Context: {context}] {q}" if context else q
    return full if len(full) <= QUESTION_LIMIT else q


# "Ask in chat" on an insight card: this question, then the card's facts (at most FACTS_MAX = 400 on the server, so the
# two always fit QUESTION_LIMIT; with_context drops the page context first if it does not).
ASK_WHY = "Why did this happen, and what should I check first? "


_MD_ANYWHERE = re.compile(r"([\\`*_{}\[\]#|<>~$&:])")
_MD_LIST = re.compile(r"^(\s*)([-+])")
_MD_NUMBERED = re.compile(r"^(\s*\d+)([.)])")


def md(text: str) -> str:
    """Data as literal text inside Markdown (campaign names, findings, problems, the viewer's own question): one line,
    and every character that could start formatting is backslash-escaped, so `[x](y)`, `*Sale*`, `# 1`, `$5K` (LaTeX),
    `:red[x]` (Streamlit colour), `&copy;` and a leading `- ` or `1. ` all show as typed. Wrap only the data: our own
    `**bold**` around it stays bold."""
    line = " ".join(str(text).split())
    return _MD_NUMBERED.sub(r"\1\\\2", _MD_LIST.sub(r"\1\\\2", _MD_ANYWHERE.sub(r"\\\1", line)))


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_ATX = re.compile(r"^\s{0,3}#{1,6}(?:\s+(.*?))?\s*#*\s*$")
_UNDERLINE = re.compile(r"^\s{0,3}(=+|-+|(?:\*\s*){3,})\s*$")


def answer_md(text: str) -> str:
    """A model's Markdown that can't take over the page. Outside code blocks: headings become bold lines, a line of
    only -, = or * gets a blank line above it (else the paragraph above renders as a heading), an odd `**` or `__`
    has its last marker escaped, and $ stays a dollar sign. Code blocks, tables, lists and links are left as written."""
    out: list[str] = []
    prose: list[int] = []  # indexes of the text lines outside code blocks (not rules), where ** and __ count
    fenced = False
    for line in (text or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            heading = _ATX.match(line)
            if heading:
                line = f"**{heading.group(1).replace('**', '')}**" if heading.group(1) else ""
            if _UNDERLINE.match(line):
                if out and out[-1].strip():
                    out.append("")
            else:
                prose.append(len(out))
            line = line.replace("$", "\\$")
        out.append(line)
    for marker in ("**", "__"):
        if sum(out[i].count(marker) for i in prose) % 2:
            i = max(i for i in prose if marker in out[i])
            at = out[i].rfind(marker)
            out[i] = f"{out[i][:at]}\\{marker[0]}\\{marker[1]}{out[i][at + 2:]}"
    return "\n".join(out)


CAVEATS = {
    "Rerun: BudgetExceeded": "The analyst hit its query limit, so this answer may be incomplete.",
    "OutputPolicy": "Part of the answer was withheld because it named internal details.",
    "GuardDegraded": "One of the question checks was unavailable, so only the basic checks ran.",
    "InputPolicy": "A safety check stopped this question.",
    "OutOfScope": "This question is outside the marketing data, so the analyst did not answer it.",
}
_CODE = re.compile(r"^[A-Za-z]+(:\s?\w+)?$")


def caveat_text(caveat: str) -> str:
    """The API's caveats are machine codes the evals and the audit read (unchanged there); viewers get a sentence.
    A sentence the analyst wrote is shown as it is."""
    if caveat in CAVEATS:
        return CAVEATS[caveat]
    if caveat.startswith("Rerun: "):
        return "The first attempt failed, so the analyst answered on a second try."
    if caveat.startswith("classifier:"):
        return CAVEATS["InputPolicy"]
    if "answered without the language model" in caveat:
        return "The language model was unavailable, so this answer comes from a pre-defined query."
    if _CODE.match(caveat):
        return "The analyst noted a limitation with this answer."
    return caveat
