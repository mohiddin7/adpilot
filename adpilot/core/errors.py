"""Single error type for everything the agent can surface to a model or a user."""

from __future__ import annotations

from typing import Literal

ErrorKind = Literal[
    "SqlSyntax",
    "SqlSchema",
    "SqlPolicy",
    "InputPolicy",
    "OutputPolicy",
    "DataSourceUnavailable",
    "QueryTimeout",
    "ModelRateLimited",
    "ModelUnavailable",
    "OutOfScope",
    "BudgetExceeded",
]


class AdPilotError(Exception):
    """Typed, user-safe error. `hint` is advice the model can act on; `layer` names the guard that raised it
    (e.g. "classifier:jev") so trip rates per layer are queryable from the caveats in the audit trail."""

    def __init__(self, kind: ErrorKind, message: str, hint: str = "", layer: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.layer = layer
