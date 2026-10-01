"""Pure view logic for the dashboard pages: date presets, widget state → API query pairs, KPI deltas, the chat context
line. No Streamlit import, so every rule a page relies on has a plain unit test (tests/test_dashboard_controls.py)."""

from __future__ import annotations

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


INVESTIGATE = "Why did this happen, and what should I check first? "


def investigate_question(facts: str, limit: int = QUESTION_LIMIT) -> str:
    """A card's facts as a question to the analyst, trimmed to fit; with_context then drops the page context first."""
    q = INVESTIGATE + " ".join(facts.split())
    return q if len(q) <= limit else q[: limit - 3].rstrip() + "..."


def md(text: str) -> str:
    """Streamlit markdown renders $...$ as LaTeX; dollar amounts must stay dollar amounts."""
    return text.replace("$", "\\$")
