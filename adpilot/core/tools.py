"""Typed tools. Every data-source failure comes back as an SqlError *result* so the model can repair in-loop."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from adpilot.connectors.base import DataSource
from adpilot.core.audit import AuditSink, RunContextInfo
from adpilot.core.chart import ChartSpec, validate_spec
from adpilot.core.errors import AdPilotError, ErrorKind
from adpilot.core.guardrails import Budget, validate_sql
from adpilot.packs.loader import Pack


@dataclass
class AgentDeps:
    connector: DataSource
    pack: Pack
    schema_text: str
    audit: AuditSink
    budget: Budget = field(default_factory=Budget)
    last_result: pd.DataFrame | None = None
    results: list[SqlResult] = field(default_factory=list)  # every successful query this turn, in order — the judge grades against all of them
    run_context: RunContextInfo | None = None


class SqlResult(BaseModel):
    sql: str
    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool = False
    note: str = ""


# Without this the model read "no rows" from a pipeline table as a bad query and searched until it ran out of calls.
EMPTY_PIPELINE_NOTE = (
    "No rows: the pipeline found nothing, or its output table has not been populated yet. That is the answer: "
    "give a normal answer (not a refusal — the question is in scope) saying no pipeline output is available, "
    "Do not query further for it, and do not recompute it from other tables: the pipeline's method is not yours."
)


class SqlError(BaseModel):
    kind: ErrorKind
    message: str
    hint: str = ""
    columns: list[str] = []


# ponytail: one lock — pydantic-ai runs parallel tool calls in threads and DuckDB connections are not thread-safe
_EXEC_LOCK = threading.Lock()


def execute(deps: AgentDeps, sql: str, max_rows: int | None = None) -> SqlResult | SqlError:
    """Budget → validate → run. Shared by run_sql, the tier-2 tools, the rule-based fallback and dashboard panels.

    `max_rows` raises the row cap for pack-authored dashboard queries (a 90-day trend has more than 100 rows); the
    model-facing tools never pass it."""
    pack, con = deps.pack, deps.connector
    limit = max_rows or pack.max_result_rows
    with _EXEC_LOCK:
        try:
            deps.budget.take_sql()
            clean = validate_sql(sql, pack.allowed_tables(con.dialect), limit)
            df = con.query(clean, max_bytes=pack.raw.get("max_bytes_billed"))
        except AdPilotError as exc:
            cols = [c for c, _ in _gold_columns(deps)] if exc.kind == "SqlSchema" else []
            return SqlError(kind=exc.kind, message=exc.message, hint=exc.hint, columns=cols)
        deps.last_result = df
    rows = df.head(limit)
    result = SqlResult(
        sql=clean,
        columns=list(df.columns),
        rows=records(rows),
        row_count=len(df),
        truncated=len(df) > len(rows),
    )
    deps.results.append(result)
    return result


def records(df: pd.DataFrame) -> list[dict]:
    out = df.copy()
    for col in out.columns:
        if str(out[col].dtype).startswith(("datetime", "date")):
            out[col] = out[col].astype(str)
    return [{k: (None if pd.isna(v) else v) for k, v in rec.items()} for rec in out.to_dict("records")]


def _gold_columns(deps: AgentDeps) -> list[tuple[str, str]]:
    try:
        return deps.connector.columns(deps.pack.table_ref("gold", deps.connector.dialect))
    except AdPilotError:
        return [(c, "") for c in deps.pack.tables["gold"].get("columns", {})]


def _pipeline(deps: AgentDeps, sql: str) -> SqlResult | SqlError:
    """A pipeline-output table (anomalies, forecast, budget): an empty result says why, and that it is final."""
    res = execute(deps, deps.pack.render(sql, deps.connector.dialect))
    if isinstance(res, SqlResult) and not res.rows:
        res.note = EMPTY_PIPELINE_NOTE
    return res


def register_tools(agent: Agent[AgentDeps, object]) -> None:
    @agent.tool
    def get_schema(ctx: RunContext[AgentDeps]) -> str:
        """List the queryable tables and their columns."""
        return ctx.deps.schema_text

    @agent.tool
    def run_sql(ctx: RunContext[AgentDeps], sql: str) -> SqlResult | SqlError:
        """Run one read-only SELECT. On error, fix the SQL using `hint`/`columns` and call again (max 3 runs)."""
        return execute(ctx.deps, sql)

    @agent.tool
    def get_anomalies(ctx: RunContext[AgentDeps], limit: int = 20) -> SqlResult | SqlError:
        """Most severe cost-per-acquisition anomalies (highest |z_score|) flagged by the pipeline."""
        sql = (
            "SELECT date, platform, campaign_name, observed_cpa, rolling_mean_cpa, z_score, anomaly_direction "
            "FROM {anomalies} WHERE is_anomaly = 1 ORDER BY ABS(z_score) DESC LIMIT " + str(max(1, min(limit, 100)))
        )
        return _pipeline(ctx.deps, sql)

    @agent.tool
    def get_forecast(ctx: RunContext[AgentDeps], platform: str | None = None) -> SqlResult | SqlError:
        """14-day forecast rows (predicted_value with bounds) per platform and metric."""
        where = f" WHERE platform = '{platform.replace(chr(39), '')}'" if platform else ""
        sql = "SELECT target_date, platform, metric_name, predicted_value, lower_bound, upper_bound FROM {forecast}" + where + " ORDER BY platform, metric_name, target_date"
        return _pipeline(ctx.deps, sql)

    @agent.tool
    def get_budget_plan(ctx: RunContext[AgentDeps]) -> SqlResult | SqlError:
        """Budget optimizer recommendation: current vs recommended spend per platform and projected conversion delta."""
        sql = "SELECT platform, current_spend, current_spend_pct, recommended_spend, recommended_spend_pct, projected_conversions, conversion_delta FROM {budget} ORDER BY conversion_delta DESC"
        return _pipeline(ctx.deps, sql)

    @agent.tool
    def render_chart(ctx: RunContext[AgentDeps], spec: ChartSpec) -> ChartSpec | SqlError:
        """Validate a chart spec against the columns of the last query result. Put the returned spec in the answer's `chart`."""
        df = ctx.deps.last_result
        if df is None or df.empty:
            return SqlError(kind="SqlSchema", message="No query result to chart yet. Run a query first.")
        try:
            return validate_spec(spec, list(df.columns))
        except ValueError as exc:
            return SqlError(kind="SqlSchema", message=str(exc), columns=list(df.columns))
