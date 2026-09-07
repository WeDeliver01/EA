"""Property-based version of the transition-table exhaustiveness test, using
Hypothesis to sample (current, target) pairs rather than the parametrised
cross-product - belt and braces on the same invariant."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.exceptions import IllegalStateTransition
from app.domain.execution.enums import ExecutionState
from app.domain.execution.state_machine import allowed_transitions, assert_transition

pytestmark = pytest.mark.unit

states = st.sampled_from(list(ExecutionState))


@given(current=states, target=states)
def test_transition_raises_iff_disallowed(current: ExecutionState, target: ExecutionState) -> None:
    is_allowed = target in allowed_transitions(current)
    if is_allowed:
        assert_transition(current, target)
    else:
        with pytest.raises(IllegalStateTransition):
            assert_transition(current, target)
