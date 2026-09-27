"""Loads a pack directory: pack.yaml + glossary.md + prompts/system.md."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Pack:
    name: str
    root: Path
    raw: dict
    glossary: str
    system_prompt: str

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def tables(self) -> dict[str, dict]:
        return self.raw["tables"]

    @property
    def max_result_rows(self) -> int:
        return int(self.raw.get("max_result_rows", 100))

    @cached_property
    def prompt_hash(self) -> str:
        """12-hex sha256 over system prompt + glossary + the tables block: changes when the agent's context changes."""
        tables = yaml.safe_dump(self.raw.get("tables", {}), sort_keys=True)
        return hashlib.sha256((self.system_prompt + "\n" + self.glossary + "\n" + tables).encode()).hexdigest()[:12]

    def table_ref(self, logical: str, connector: str) -> str:
        template = self.tables[logical][connector]
        if connector != "bigquery":
            return template
        bq = self.raw["bigquery"]
        return template.format(
            project=os.environ.get(bq["project_env"], bq.get("project_default", "")),
            staging=os.environ.get(bq["staging_env"], bq.get("staging_default", "")),
            production=os.environ.get(bq["production_env"], bq.get("production_default", "")),
        )

    def allowed_tables(self, connector: str) -> set[str]:
        return {self.table_ref(name, connector) for name in self.tables}

    def render(self, sql: str, connector: str, **extra: str) -> str:
        """Replace {gold}, {anomalies}, ... with physical references, plus any `extra` placeholders ({where})."""
        return sql.format(**{name: self.table_ref(name, connector) for name in self.tables}, **extra)


def load_pack(name_or_path: str = "ads") -> Pack:
    root = Path(name_or_path)
    if not root.is_dir():
        root = REPO_ROOT / "packs" / name_or_path
    raw = yaml.safe_load((root / "pack.yaml").read_text())
    return Pack(
        name=raw["name"],
        root=root,
        raw=raw,
        glossary=(root / "glossary.md").read_text(),
        system_prompt=(root / "prompts" / "system.md").read_text(),
    )
