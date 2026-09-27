"""Dashboard reads: every panel, filter-option and data-window query runs through the same execute() the agent's
run_sql tool uses (budget → validate_sql → connector), each on a fresh per-call copy of the shared deps."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Hashable

from pydantic import BaseModel

from adpilot.brief import load, settings
from adpilot.brief.analyses import pace_numbers
from adpilot.core.chart import ChartSpec, validate_spec
from adpilot.core.errors import AdPilotError
from adpilot.core.runtime import fresh_deps
from adpilot.core.tools import AgentDeps, SqlError, execute
from adpilot.dashboard.config import DashboardConfig, FilterDef, PanelDef
from adpilot.dashboard.filters import Filters, build_where

log = logging.getLogger(__name__)

OPTIONS_MAX_ROWS = 500
# Kinds our own guards raise with fixed, safe-to-show text (adpilot/core/guardrails.py). Everything else reaching
# here (SqlSchema, SqlSyntax, DataSourceUnavailable) comes from a connector and can carry the live project:dataset.table.
_SAFE_ERROR_KINDS = {"SqlPolicy", "BudgetExceeded"}


def _safe_error(kind: str, message: str) -> str:
    if kind in _SAFE_ERROR_KINDS:
        return f"{kind}: {message}"
    log.warning("dashboard query failed: %s: %s", kind, message)
    return f"{kind}: this panel could not be read"


class TtlCache:
    """ponytail: one in-process dict, cleared whole when it reaches max_entries. Per instance and lost on
    scale-to-zero, which is fine for data the pipeline refreshes once a day; a shared cache only if several instances
    start serving the same viewers."""

    def __init__(self, ttl_s: float, max_entries: int = 256, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s, self.max_entries, self.clock = ttl_s, max_entries, clock
        self._data: dict[Hashable, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable):
        with self._lock:
            hit = self._data.get(key)
            return hit[1] if hit and hit[0] > self.clock() else None

    def put(self, key: Hashable, value: object) -> None:
        if self.ttl_s <= 0:
            return
        with self._lock:
            if len(self._data) >= self.max_entries:
                self._data.clear()
            self._data[key] = (self.clock() + self.ttl_s, value)


class PanelResult(BaseModel):
    id: str
    title: str
    kind: str
    table: str
    chart: ChartSpec | None = None
    columns: list[str] = []
    rows: list[dict] = []
    truncated: bool = False
    note: str = ""
    error: str | None = None


def panel_applies(panel: PanelDef, flt: Filters) -> bool:
    """A platform-specific panel (quality score, video funnel) shows only when every selected platform is one it is
    for — averaging Google's quality score with Facebook's zeros would be a wrong number, not a smaller one."""
    if not panel.platforms:
        return True
    chosen = set(flt.values("platform"))
    return bool(chosen) and chosen <= set(panel.platforms)


def run_panel(template: AgentDeps, cfg: DashboardConfig, panel: PanelDef, flt: Filters) -> PanelResult:
    """One panel. Every failure — a value the SQL policy rejects, a bytes-billed cap, a chart naming a column the
    query did not return — comes back as this panel's `error`, so one bad panel never blanks the page."""
    deps = fresh_deps(template)  # a fresh SQL budget per panel: a page has more panels than a question has queries
    dialect = deps.connector.dialect
    result = PanelResult(id=panel.id, title=panel.title, kind=panel.kind, table=panel.table)
    where = build_where(cfg, panel.table, deps.pack.raw["date_column"], flt, dialect)
    res = execute(deps, deps.pack.render(panel.sql, dialect, where=where), max_rows=cfg.max_rows)
    if isinstance(res, SqlError):
        return result.model_copy(update={"error": _safe_error(res.kind, res.message)})
    chart = None
    if panel.chart is not None:
        try:
            chart = validate_spec(panel.chart, res.columns)
        except ValueError as exc:
            return result.model_copy(update={"error": f"chart: {exc}"})
    return result.model_copy(update={
        "chart": chart, "columns": res.columns, "rows": res.rows, "truncated": res.truncated,
        "note": "" if res.rows else "No rows for this selection.",
    })


def run_page(template: AgentDeps, cfg: DashboardConfig, page: str, flt: Filters, cache: TtlCache,
             ids: tuple[str, ...] = ()) -> list[PanelResult]:
    """The page's panels that apply to this selection (or only `ids`), in pack order. Cached per panel, and only
    when that panel succeeded, so one always-failing panel is retried every load without evicting the rest."""
    wanted = [p for p in cfg.panels_for(page) if (not ids or p.id in ids) and panel_applies(p, flt)]
    out = []
    for p in wanted:
        key = ("panel", p.id, flt)
        result = cache.get(key)
        if result is None:
            result = run_panel(template, cfg, p, flt)
            if result.error is None:
                cache.put(key, result)
        out.append(result)
    return out


def filter_options(template: AgentDeps, cfg: DashboardConfig, page: str, flt: Filters,
                   cache: TtlCache) -> dict[str, dict]:
    """Per filter declared for the page: {"values": [...]} or {"min": x, "max": y}. A categorical filter's options are
    narrowed by the date range and its ancestors only (platform → campaign → ad set), so choosing a campaign never
    empties the campaign list itself; a range's bounds follow every categorical choice."""
    key = ("filters", page, dataclasses.replace(flt, ranges=()))  # option queries never read ranges
    hit = cache.get(key)
    if hit is not None:
        return hit
    categorical = {f.column for f in cfg.filters if f.type == "categorical"}
    out: dict[str, dict] = {}
    failed = False
    for f in cfg.filters_for(page):
        if f.values is not None:
            out[f.column] = {"values": list(f.values)}
            continue
        option = _option(template, cfg, f, flt, _ancestors(cfg, f) if f.type == "categorical" else categorical)
        failed = failed or "error" in option
        out[f.column] = option
    if not failed:
        cache.put(key, out)
    return out


def _ancestors(cfg: DashboardConfig, f: FilterDef) -> set[str]:
    seen: set[str] = set()
    parent = f.depends_on
    while parent is not None and parent not in seen:
        seen.add(parent)
        parent = cfg.filter(parent).depends_on
    return seen


def _option(template: AgentDeps, cfg: DashboardConfig, f: FilterDef, flt: Filters, only: set[str]) -> dict:
    deps = fresh_deps(template)
    dialect, table = deps.connector.dialect, f.tables[0]
    where = build_where(cfg, table, deps.pack.raw["date_column"], flt, dialect, only=only)
    if f.type == "categorical":
        sql = f"SELECT DISTINCT {f.column} AS value FROM {{{table}}} WHERE {{where}} ORDER BY 1"
    else:
        sql = f"SELECT MIN({f.column}) AS lo, MAX({f.column}) AS hi FROM {{{table}}} WHERE {{where}}"
    res = execute(deps, deps.pack.render(sql, dialect, where=where), max_rows=OPTIONS_MAX_ROWS)
    if isinstance(res, SqlError):
        empty = {"values": []} if f.type == "categorical" else {}
        return {**empty, "error": _safe_error(res.kind, res.message)}
    if f.type == "categorical":
        return {"values": [r["value"] for r in res.rows if r["value"] is not None], "truncated": res.truncated}
    row = res.rows[0] if res.rows else {}
    return {"min": row.get("lo"), "max": row.get("hi")}


def dashboard_meta(template: AgentDeps, cfg: DashboardConfig, cache: TtlCache) -> dict:
    """What the Streamlit app needs to draw its controls without reading pack.yaml: the data window (date presets
    count back from the newest day, not from today), the declared filters, panel titles and insight questions.
    Panel SQL never leaves the server."""
    hit = cache.get(("meta",))
    if hit is not None:
        return hit
    deps = fresh_deps(template)
    pack, dialect = deps.pack, deps.connector.dialect
    dc = pack.raw["date_column"]
    res = execute(deps, pack.render(f"SELECT MIN({dc}) AS lo, MAX({dc}) AS hi FROM {{gold}}", dialect))
    if isinstance(res, SqlError) or not res.rows or res.rows[0]["hi"] is None:
        raise AdPilotError("DataSourceUnavailable", "could not read the data window from the gold table")
    meta = {
        "pack": pack.name,
        "date_column": dc,
        "date_min": str(res.rows[0]["lo"])[:10],  # a date, a datetime or a string, depending on the connector
        "date_max": str(res.rows[0]["hi"])[:10],
        "filters": [f.model_dump(exclude_none=True) for f in cfg.filters],
        "panels": {
            page: [{"id": p.id, "title": p.title, "kind": p.kind, "table": p.table, "platforms": p.platforms}
                   for p in cfg.panels_for(page)]
            for page in ("overview", "deep_dive")
        },
        "insights": list(cfg.insights),
    }
    cache.put(("meta",), meta)
    return meta


def _safe_problems(problems: list[str]) -> list[str]:
    """brief.load's problems are `"couldn't read the <name> table (<exc>)"`; the parenthetical can carry the live
    project:dataset.table, so viewers get the fixed sentence and the original goes to the log only."""
    out = []
    for p in problems:
        head, sep, _tail = p.partition(" (")
        if sep:
            log.warning("pacing problem: %s", p)
        out.append(head)
    return out


def pacing_rows(template: AgentDeps, cache: TtlCache) -> dict:
    """Month-end pacing computed by the daily brief's own code (brief.load + analyses.pace_numbers), so the dashboard
    and the brief can never disagree about the same month. Whole account, anchored on the newest day in the data."""
    hit = cache.get(("pacing",))
    if hit is not None:
        return hit
    try:
        t, budgets = settings(template.pack)
        data, problems = load(fresh_deps(template), t)
    except ValueError as exc:  # settings() rejects pack.yaml config with our own text: safe to show as-is
        return {"as_of": None, "rows": [], "problems": [str(exc)]}
    except AdPilotError as exc:  # a connector failure reading the gold table: message may name it
        log.warning("pacing setup failed: %s: %s", exc.kind, exc.message)
        return {"as_of": None, "rows": [], "problems": [f"{exc.kind}: couldn't read the pacing data"]}
    if "gold" not in data:
        return {"as_of": None, "rows": [], "problems": _safe_problems(problems)}
    rows = [
        {
            "platform": plat,
            "budget": n["budget"],
            "spent_mtd": round(n["spent_mtd"], 2),
            "projected": None if n["projected"] is None else round(n["projected"], 2),
            "off_pct": None if n["budget"] is None else round((n["projected"] / n["budget"] - 1) * 100, 1),
            "days_left": n["days_left"],
            "basis": n["basis"],
        }
        for plat, n in pace_numbers(data["gold"], data.get("forecast"), budgets, data["latest"]).items()
    ]
    out = {"as_of": data["latest"].isoformat(), "rows": rows, "problems": _safe_problems(problems)}
    if not problems:
        cache.put(("pacing",), out)
    return out
