from __future__ import annotations

from enum import StrEnum


class MatterStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class RouteStatus(StrEnum):
    IN_SCOPE = "IN_SCOPE"
    NEED_SCOPE_CONFIRMATION = "NEED_SCOPE_CONFIRMATION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class MatterStateError(RuntimeError):
    pass


class DuplicateSubmissionError(MatterStateError):
    pass
