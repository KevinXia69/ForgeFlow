"""One outward vocabulary: the seven user states, with the finer phase beside them."""
from __future__ import annotations

import pytest

from forgeflow.superagent import (
    USER_TICKET_STATE,
    USER_TICKET_STATES,
    normalize_ticket_state,
    phase_for,
    user_state_for,
)


def test_every_ticket_state_maps_into_the_seven_state_contract() -> None:
    from forgeflow.superagent.contracts import TicketState

    for state in TicketState:
        mapped = user_state_for(state.value)
        assert mapped in USER_TICKET_STATES, state


def test_legacy_states_are_normalised_before_display() -> None:
    assert normalize_ticket_state("waiting_review") == "result_ready"
    assert normalize_ticket_state("needs_attention") == "blocked"
    assert user_state_for("waiting_review") == "investigating"
    assert user_state_for("needs_attention") == "needs_action"


@pytest.mark.parametrize("state", ["cancelled", "closed", "failed", "blocked"])
def test_unfinished_or_unknown_states_never_render_as_completed(state: str) -> None:
    """A stop, a failure or an unknown result is not "done"."""
    assert user_state_for(state) == "needs_action"


def test_phase_explains_where_the_work_is_without_replacing_the_outcome() -> None:
    assert user_state_for("cancelled") == "needs_action"
    assert phase_for("cancelled") == "stopped"
    assert user_state_for("completed") == "completed"
    assert phase_for("completed") == "verified"
    assert user_state_for("waiting_input") == "needs_input"
    assert phase_for("waiting_input") == "waiting_for_fact"
    assert phase_for("not_a_state") == "unknown"


def test_the_contract_has_exactly_seven_values() -> None:
    assert len(USER_TICKET_STATES) == 7
    assert set(USER_TICKET_STATE.values()) <= set(USER_TICKET_STATES)
