"""Constrained chart spec: the model picks a type and column names, never code. Rendering is the UI's job."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ChartSpec(BaseModel):
    chart_type: Literal["bar", "line", "scatter", "pie", "area"]
    x: str
    y: str
    color: str | None = None
    title: str = Field(default="", max_length=80)


class PanelChartSpec(ChartSpec):
    """Pack panels and /insights evidence only. The model's render_chart keeps ChartSpec's five types, so its
    tool schema never offers these. `reference: mean_y` draws the mean of y, weighted by `size` when set (on the
    efficiency map, y = CPA and size = conversions, so it is the account CPA)."""

    chart_type: Literal["bar", "line", "scatter", "pie", "area", "bubble", "funnel", "heatmap", "bar_h"]
    size: str | None = None
    z: str | None = None
    reference: Literal["mean_y"] | None = None

    @model_validator(mode="after")
    def _extra_columns(self) -> PanelChartSpec:
        if self.chart_type == "bubble" and not self.size:
            raise ValueError("a bubble chart needs `size`")
        if self.chart_type == "heatmap" and not self.z:
            raise ValueError("a heatmap needs `z`")
        return self


def validate_spec(spec: ChartSpec, columns: list[str]) -> ChartSpec:
    """Every referenced column must exist; an unknown color column is dropped rather than failing."""
    needed = [spec.x, spec.y, *(c for c in (getattr(spec, "size", None), getattr(spec, "z", None)) if c)]
    missing = [c for c in needed if c not in columns]
    if missing:
        raise ValueError(f"Column(s) {missing} not in result. Available: {', '.join(columns)}")
    if spec.color is not None and spec.color not in columns:
        spec = spec.model_copy(update={"color": None})
    return spec


def heuristic_chart(columns: list[str], numeric: list[str]) -> ChartSpec | None:
    if not numeric:
        return None
    y = numeric[0]
    if "date" in columns:
        return ChartSpec(chart_type="line", x="date", y=y, color="platform" if "platform" in columns else None, title=f"{y} over time")
    if "platform" in columns:
        return ChartSpec(chart_type="bar", x="platform", y=y, title=f"{y} by platform")
    categorical = [c for c in columns if c not in numeric]
    if categorical:
        return ChartSpec(chart_type="bar", x=categorical[0], y=y, title=f"{y} by {categorical[0]}")
    return None
