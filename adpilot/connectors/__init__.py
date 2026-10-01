from __future__ import annotations

import os
from typing import TYPE_CHECKING

from adpilot.connectors.base import DataSource

if TYPE_CHECKING:
    from adpilot.packs.loader import Pack


def get_connector(name: str, pack: Pack) -> DataSource:
    if name == "duckdb":
        from adpilot.connectors.duckdb import DuckDBSource

        cfg = pack.raw["duckdb"]
        return DuckDBSource(
            csv_dir=pack.repo_root / cfg["csv_dir"],
            init_sql=(pack.root / cfg["init_sql"]).read_text(),
            timeout_s=pack.query_timeout_s,
        )
    if name == "bigquery":
        from adpilot.connectors.bigquery import BigQuerySource

        cfg = pack.raw["bigquery"]
        return BigQuerySource(
            project=os.environ.get(cfg["project_env"], cfg.get("project_default", "adpilot-lakehouse")),
            default_max_bytes=pack.raw.get("max_bytes_billed", 10 * 1024 * 1024),
            timeout_s=pack.query_timeout_s,
        )
    raise ValueError(f"Unknown connector {name!r}; expected duckdb or bigquery")


__all__ = ["DataSource", "get_connector"]
