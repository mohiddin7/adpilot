"""Schema text for the system prompt: live column names/types + pack descriptions."""

from __future__ import annotations

from adpilot.connectors.base import DataSource
from adpilot.core.errors import AdPilotError
from adpilot.packs.loader import Pack


def summary(connector: DataSource, pack: Pack) -> str:
    blocks = []
    for logical, spec in pack.tables.items():
        ref = pack.table_ref(logical, connector.dialect)
        descs: dict[str, str] = spec.get("columns", {})
        try:
            live = connector.columns(ref)
        except AdPilotError:
            live = []
        cols = live or [(name, "") for name in descs]
        lines = [f"  {name} {ctype}".rstrip() + (f" — {descs[name]}" if name in descs else "") for name, ctype in cols]
        blocks.append(f"{ref}  ({logical})\n  {spec.get('description', '')}\n" + "\n".join(lines))
    return "\n\n".join(blocks)
