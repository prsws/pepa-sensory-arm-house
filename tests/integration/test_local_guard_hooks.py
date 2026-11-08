"""Home Assistant behavior the local handling guard depends on.

These tests pin down, against a real Home Assistant, the hook points named in
docs/LOCAL_HANDLING_GUARD.md. They use no code from this integration. If a
Home Assistant release changes any of them, these tests fail and the guard
must be checked against that release before it ships.
"""

import inspect
from pathlib import Path

import homeassistant
import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation import default_agent
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session, intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_mock_service

PEPA_AGENT_ID = "01PEPAAGENTENTRYID0000000"
LIGHT = "light.kitchen"


@pytest.fixture
async def assist_hass(hass: HomeAssistant) -> HomeAssistant:
    """Real Home Assistant with Assist, intents and one exposed light."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    hass.states.async_set(LIGHT, "off", {"friendly_name": "kitchen light"})
    async_expose_entity(hass, conversation.DOMAIN, LIGHT, True)
    return hass


async def _local_attempt(hass: HomeAssistant, text: str) -> intent.IntentResponse | None:
    """Run Home Assistant's prefer-local attempt the way the pipeline does."""
    user_input = conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id=PEPA_AGENT_ID,
    )
    with (
        chat_session.async_get_chat_session(hass, context=user_input.context) as session,
        conversation.async_get_chat_log(hass, session, user_input) as chat_log,
    ):
        return await conversation.async_handle_intents(hass, user_input, chat_log)


def test_service_handlers_expose_a_step_before_any_service_call() -> None:
    """Every matched entity reaches async_handle_states before a service runs."""
    params = list(
        inspect.signature(intent.DynamicServiceIntentHandler.async_handle_states).parameters
    )
    assert params[:4] == ["self", "intent_obj", "match_result", "match_constraints"]
    assert hasattr(intent.MatchTargetsResult, "__dataclass_fields__")
    assert "states" in intent.MatchTargetsResult.__dataclass_fields__


def test_intent_carries_the_conversation_agent_id() -> None:
    """An intent records which conversation agent the request was for."""
    assert "conversation_agent_id" in inspect.signature(intent.Intent).parameters
    assert "conversation_agent_id" in intent.Intent.__slots__


def test_no_valid_targets_hands_the_turn_on() -> None:
    """A match failure is one of the codes the local attempt discards."""
    assert intent.IntentResponseErrorCode.NO_VALID_TARGETS in default_agent._MATCH_ERROR_CODES


def test_pipeline_tries_sentence_triggers_then_local_intents_then_the_agent() -> None:
    """The pipeline order the guard and the sentence-trigger scan rely on.

    Read as text: importing assist_pipeline pulls in speech dependencies the
    test environment does not need.
    """
    source = (
        Path(homeassistant.__file__).parent / "components/assist_pipeline/default_pipeline.py"
    ).read_text(encoding="utf-8")
    triggers = source.index("conversation.async_handle_sentence_triggers(")
    local = source.index("conversation.async_handle_intents(")
    assert triggers < local
    assert "prefer_local_intents" in source[triggers : local + 200]


async def test_local_attempt_acts_when_the_handler_succeeds(assist_hass: HomeAssistant) -> None:
    """Baseline: with nothing in the way, Home Assistant acts by itself."""
    calls = async_mock_service(assist_hass, "light", "turn_on")
    response = await _local_attempt(assist_hass, "turn on the kitchen light")
    assert response is not None
    assert response.response_type is intent.IntentResponseType.ACTION_DONE
    assert len(calls) == 1


async def test_match_failure_in_the_handler_falls_through_to_the_agent(
    assist_hass: HomeAssistant,
) -> None:
    """MatchFailedError before any service call: nothing runs, the agent gets the turn.

    Also checks that the handler sees the pipeline agent's id.
    """
    calls = async_mock_service(assist_hass, "light", "turn_on")
    turn_on = next(
        h for h in intent.async_get(assist_hass) if h.intent_type == intent.INTENT_TURN_ON
    )
    seen: list[str | None] = []

    async def refuse(intent_obj, match_result, match_constraints, match_preferences=None):
        seen.append(intent_obj.conversation_agent_id)
        raise intent.MatchFailedError(result=match_result, constraints=match_constraints)

    original = turn_on.async_handle_states
    turn_on.async_handle_states = refuse
    try:
        response = await _local_attempt(assist_hass, "turn on the kitchen light")
    finally:
        turn_on.async_handle_states = original

    assert response is None
    assert calls == []
    assert seen == [PEPA_AGENT_ID]
