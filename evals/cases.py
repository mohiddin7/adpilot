"""Case schema + loader for packs/<pack>/evals.yaml, and the no-model reference check."""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, model_validator
from pydantic_evals import Case, Dataset

from adpilot.packs.loader import Pack
from evals.task import EvalInputs

Family = Literal["factual", "paraphrase", "multiturn", "redteam", "scope", "narrative"]
ACCURACY_FAMILIES = ("factual", "paraphrase", "multiturn")
SAFETY_FAMILIES = ("redteam", "scope")


class Expected(BaseModel):
    sql: str | None = None
    value: float | None = None
    column: str | None = None
    tolerance: float = 0.01
    refuse: bool | None = None
    rubric: str | None = None


class EvalCase(BaseModel):
    name: str
    family: Family
    question: str | None = None
    turns: list[str] | None = None
    tool: str | None = None
    group: str | None = None
    consistency: bool = False
    expected: Expected

    @model_validator(mode="after")
    def _shape(self) -> EvalCase:
        if not (self.question or self.turns):
            raise ValueError(f"{self.name}: needs question or turns")
        if self.family in ACCURACY_FAMILIES and not self.expected.sql:
            raise ValueError(f"{self.name}: {self.family} needs expected.sql")
        if self.family in SAFETY_FAMILIES and self.expected.refuse is None:
            raise ValueError(f"{self.name}: {self.family} needs expected.refuse")
        if self.family == "narrative" and not self.expected.rubric:
            raise ValueError(f"{self.name}: narrative needs expected.rubric")
        return self

    @property
    def inputs(self) -> EvalInputs:
        return EvalInputs(name=self.name, question=self.question, turns=self.turns)

    @property
    def metadata(self) -> dict[str, Any]:
        return {"family": self.family, "tool": self.tool, "group": self.group, "consistency": self.consistency}


def load_cases(pack: Pack, families: set[str] | None = None, limit: int | None = None) -> list[EvalCase]:
    raw = yaml.safe_load((pack.root / "evals.yaml").read_text())
    cases = [EvalCase(**c) for c in raw["cases"]]
    names = [c.name for c in cases]
    if len(set(names)) != len(names):
        raise ValueError("duplicate case names in evals.yaml")
    if families:
        cases = [c for c in cases if c.family in families]
    return cases[:limit] if limit else cases


def to_dataset(cases: list[EvalCase], evaluators: list) -> Dataset[EvalInputs, Any, dict]:
    return Dataset(
        name="adpilot",
        cases=[Case(name=c.name, inputs=c.inputs, expected_output=c.expected, metadata=c.metadata) for c in cases],
        evaluators=evaluators,
    )


def reference_rows(connector: Any, pack: Pack, expected: Expected) -> list[dict]:
    from adpilot.core.tools import records

    if not expected.sql:
        return []
    return records(connector.query(pack.render(expected.sql, connector.dialect)))


def check_cases(connector: Any, pack: Pack, cases: list[EvalCase]) -> list[str]:
    problems: list[str] = []
    for c in cases:
        if not c.expected.sql:
            continue
        try:
            rows = reference_rows(connector, pack, c.expected)
        except Exception as exc:  # noqa: BLE001 — report every broken reference
            problems.append(f"{c.name}: reference SQL failed: {exc}")
            continue
        if c.expected.value is None:
            continue
        if not rows or c.expected.column not in rows[0]:
            problems.append(f"{c.name}: column {c.expected.column!r} not in reference result")
            continue
        got = float(rows[0][c.expected.column])
        if abs(got - c.expected.value) > c.expected.tolerance * max(abs(c.expected.value), 1e-9):
            problems.append(f"{c.name}: stored value {c.expected.value} != reference {got}")
    return problems
