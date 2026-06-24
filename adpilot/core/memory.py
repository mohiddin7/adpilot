"""Session history in SQLite (stdlib). One row per agent run, messages stored as pydantic-ai JSON."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage


class SessionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(path)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY, session_id TEXT, ts REAL, messages_json BLOB)"
        )

    def load(self, session_id: str, turns: int = 3) -> list[ModelMessage]:
        rows = self._con.execute(
            "SELECT messages_json FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, turns)
        ).fetchall()
        history: list[ModelMessage] = []
        for (blob,) in reversed(rows):
            history.extend(ModelMessagesTypeAdapter.validate_json(blob))
        return history

    def save(self, session_id: str, messages: list[ModelMessage]) -> None:
        self._con.execute(
            "INSERT INTO turns (session_id, ts, messages_json) VALUES (?, ?, ?)",
            (session_id, time.time(), ModelMessagesTypeAdapter.dump_json(messages)),
        )
        self._con.commit()
