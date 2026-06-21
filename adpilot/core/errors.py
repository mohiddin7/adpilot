"""Single error type for everything the agent can surface to a model or a user."""

from __future__ import annotations

from typing import Literal

ErrorKind = Literal[
    "SqlSyntax",
    "SqlSchema",
    "SqlPolicy",
    "DataSourceUnavailable",
    "ModelRateLimited",
    "ModelUnavailable",
    "OutOfScope",
    "BudgetExceeded",
]


class AdPilotError(Exception):
    """Typed, user-safe error. `hint` is advice the model can act on."""

    def __init__(self, kind: ErrorKind, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
