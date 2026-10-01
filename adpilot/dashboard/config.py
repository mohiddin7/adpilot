"""The pack's `dashboard:` section, parsed and checked once, when the API starts.

A typo in pack.yaml — a filter on a column the table does not have, a chart on a `kind: kpi` panel, a placeholder the
SQL cannot fill — fails the API at boot, not on somebody's first page load. Whether a chart's columns (or a panel's
`formats` keys) match what its SQL returns can only be known by running it, so that is checked per request
(panels.run_panel) and by tests/test_dashboard_panels.py, which runs every panel of the ads pack on DuckDB in CI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from adpilot.core.chart import PanelChartSpec
from adpilot.packs.loader import Pack

Page = Literal["overview", "deep_dive"]
IDENT = r"^[A-Za-z_][A-Za-z0-9_]*$"
Role = Literal["kpi", "kpi_series", "trend", "map", "compare", "funnel", "heatmap", "platform", "attention",
               "markers", "details"]
Format = Literal["currency", "percent", "multiple", "number"]
SeriesColor = Literal["brand", "forecast", "audited", "resolved"]  # never the alarm colours (critical, open)


class FilterDef(BaseModel):
    model_config = {"extra": "forbid"}

    column: str = Field(pattern=IDENT)
    label: str
    type: Literal["categorical", "range"]
    tables: list[str] = Field(min_length=1)  # logical tables the filter applies to; the column must exist in each
    pages: list[Page] = Field(min_length=1)
    values: list[str] | None = None  # a fixed allowlist: anything else is a 422, and no options query runs
    depends_on: str | None = None  # the cascade: options narrow by the parent's selection
    platforms: list[str] | None = None  # shown only when every selected platform is one of these


class PanelDef(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    page: Page
    kind: Literal["kpi", "chart", "table"]
    role: Role
    table: str = "gold"  # decides which filters {where} applies
    sql: str
    chart: PanelChartSpec | None = None
    formats: dict[str, Format] = {}
    platforms: list[str] | None = None  # a platform-specific panel (quality score, video funnel)

    @model_validator(mode="after")
    def _chart_only_on_chart_panels(self) -> PanelDef:
        if (self.kind == "chart") != (self.chart is not None):
            raise ValueError(f"panel {self.id}: a `kind: chart` panel needs `chart`, and only it may have one")
        return self


class DashboardConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_rows: int = Field(default=2000, ge=1, le=10000)
    cache_ttl_s: int = Field(default=900, ge=0)
    max_bytes_billed: int | None = Field(default=None, ge=1)  # None: the pack's (chat) cap
    filters: list[FilterDef]
    panels: list[PanelDef]
    insights: list[str] = []
    colors: dict[str, SeriesColor] = {}

    def filter(self, column: str) -> FilterDef:
        return next(f for f in self.filters if f.column == column)

    def filters_for(self, page: str) -> list[FilterDef]:
        return [f for f in self.filters if page in f.pages]

    def panels_for(self, page: str) -> list[PanelDef]:
        return [p for p in self.panels if p.page == page]


def load_dashboard(pack: Pack) -> DashboardConfig:
    raw = pack.raw.get("dashboard")
    if not raw:
        raise ValueError(f"pack {pack.name!r} has no `dashboard:` section")
    cfg = DashboardConfig.model_validate(raw)
    _check_against_pack(cfg, pack)
    return cfg


def _check_against_pack(cfg: DashboardConfig, pack: Pack) -> None:
    tables = pack.tables
    date_column = pack.raw["date_column"]
    columns = [f.column for f in cfg.filters]
    if len(columns) != len(set(columns)):
        raise ValueError("dashboard: a filter column is declared twice")
    ids = [p.id for p in cfg.panels]
    if len(ids) != len(set(ids)):
        raise ValueError("dashboard: a panel id is declared twice")
    # A typo in a `platforms:` list ("Gogle") would otherwise just hide that filter/panel forever.
    platform_filter = next((f for f in cfg.filters if f.column == "platform"), None)
    allowed_platforms = set(platform_filter.values) if platform_filter and platform_filter.values else None
    for plat in cfg.colors:
        if allowed_platforms is not None and plat not in allowed_platforms:
            raise ValueError(f"dashboard colors: platform {plat!r} is not one of {sorted(allowed_platforms)}")
    for f in cfg.filters:
        for t in f.tables:
            if t not in tables:
                raise ValueError(f"filter {f.column}: unknown table {t!r}")
            if f.column not in tables[t].get("columns", {}):
                raise ValueError(f"filter {f.column}: not a column of table {t!r}")
        if f.depends_on is not None and f.depends_on not in columns:
            raise ValueError(f"filter {f.column}: depends_on {f.depends_on!r} is not a declared filter")
        if f.values is not None and f.type != "categorical":
            raise ValueError(f"filter {f.column}: only a categorical filter can have `values`")
        if allowed_platforms is not None:
            for plat in f.platforms or []:
                if plat not in allowed_platforms:
                    raise ValueError(f"filter {f.column}: platform {plat!r} is not one of {sorted(allowed_platforms)}")
    for p in cfg.panels:
        if p.table not in tables:
            raise ValueError(f"panel {p.id}: unknown table {p.table!r}")
        if "{where}" in p.sql and date_column not in tables[p.table].get("columns", {}):
            raise ValueError(f"panel {p.id}: uses {{where}} but {p.table!r} has no {date_column!r} column")
        try:
            p.sql.format(**{t: t for t in tables}, where="1 = 1")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"panel {p.id}: bad placeholder in sql ({exc})") from exc
        if allowed_platforms is not None:
            for plat in p.platforms or []:
                if plat not in allowed_platforms:
                    raise ValueError(f"panel {p.id}: platform {plat!r} is not one of {sorted(allowed_platforms)}")
