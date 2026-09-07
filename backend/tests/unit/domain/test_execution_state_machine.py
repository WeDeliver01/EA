"""SPEC-10 Phase 1 acceptance: every disallowed execution state transition
raises IllegalStateTransition."""

from __future__ import annotations

import pytest

from app.domain.exceptions import IllegalStateTransition
from app.domain.execution.enums import ExecutionState
from app.domain.execution.state_machine import (
    TERMINAL_STATES,
    allowed_transitions,
    assert_transition,
)

pytestmark = pytest.mark.unit

ALL_STATES = list(ExecutionState)


@pytest.mark.parametrize("current", ALL_STATES)
def test_allowed_transitions_never_raise(current: ExecutionState) -> None:
    for target in allowed_transitions(current):
        assert_transition(current, target)  # must not raise


@pytest.mark.parametrize("current", ALL_STATES)
def test_disallowed_transitions_always_raise(current: ExecutionState) -> None:
    allowed = allowed_transitions(current)
    for target in ALL_STATES:
        if target in allowed:
            continue
        with pytest.raises(IllegalStateTransition):
            assert_transition(current, target)


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_outbound_transitions(state: ExecutionState) -> None:
    assert allowed_transitions(state) == frozenset()


def test_every_state_is_reachable_or_terminal() -> None:
    # Every ExecutionState appears either as a source with transitions or as
    # a declared terminal state - nothing is silently unmapped.
    from app.domain.execution.state_machine import _ALLOWED

    assert set(_ALLOWED.keys()) == set(ALL_STATES)
