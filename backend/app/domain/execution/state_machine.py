"""SPEC-01 §6: the execution state machine.

This is the authoritative transition table. Any transition not listed here is
a bug and must raise IllegalStateTransition.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.exceptions import IllegalStateTransition
from app.domain.execution.enums import ExecutionState

_S = ExecutionState

_ALLOWED: Mapping[ExecutionState, frozenset[ExecutionState]] = {
    _S.DETECTED: frozenset({_S.VALIDATING, _S.RISK_BLOCKED}),
    _S.VALIDATING: frozenset({_S.APPROVED, _S.RISK_BLOCKED, _S.REJECTED}),
    _S.APPROVED: frozenset({_S.QUEUED, _S.RISK_BLOCKED, _S.EXPIRED}),
    _S.QUEUED: frozenset({_S.SENT, _S.CANCELLED, _S.EXPIRED}),
    # CANCELLED is reachable from SENT (not just QUEUED): SPEC-06 §5 step 19,
    # the execution guard re-checks the fast gates immediately before the
    # broker call, after the intent is already marked SENT (step 16) - a
    # gate tripping in that window cancels the order before it ever reaches
    # the broker.
    _S.SENT: frozenset({_S.ACKNOWLEDGED, _S.REJECTED, _S.BROKER_ERROR, _S.UNKNOWN, _S.CANCELLED}),
    _S.ACKNOWLEDGED: frozenset(
        {_S.PARTIALLY_FILLED, _S.FILLED, _S.CANCELLED, _S.EXPIRED, _S.BROKER_ERROR}
    ),
    _S.PARTIALLY_FILLED: frozenset({_S.PARTIALLY_FILLED, _S.FILLED, _S.CANCELLED}),
    _S.FILLED: frozenset({_S.POSITION_OPEN}),
    _S.POSITION_OPEN: frozenset({_S.CLOSING, _S.CLOSED}),
    _S.CLOSING: frozenset({_S.CLOSED, _S.POSITION_OPEN}),  # close attempt failed, still open
    _S.UNKNOWN: frozenset(
        {_S.ACKNOWLEDGED, _S.FILLED, _S.REJECTED, _S.CANCELLED, _S.BROKER_ERROR}
    ),  # resolved only by reconciliation
    # Terminal states: no outbound transitions.
    _S.CLOSED: frozenset(),
    _S.RISK_BLOCKED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.BROKER_ERROR: frozenset(),
}

TERMINAL_STATES: frozenset[ExecutionState] = frozenset(
    {_S.CLOSED, _S.RISK_BLOCKED, _S.REJECTED, _S.CANCELLED, _S.EXPIRED, _S.BROKER_ERROR}
)


def allowed_transitions(current: ExecutionState) -> frozenset[ExecutionState]:
    return _ALLOWED.get(current, frozenset())


def assert_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in _ALLOWED.get(current, frozenset()):
        raise IllegalStateTransition(current, target)
