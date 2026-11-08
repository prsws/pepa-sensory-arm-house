"""Local handling guard against a real Home Assistant: device actions."""

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_mock_service

from custom_components.pepa_sensory_arm_house.local_guard import (
    async_all_reversible,
    async_get_local_guard,
    async_guard_registered_handlers,
)

PEPA_AGENT_ID = "01PEPAAGENTENTRYID0000000"
OTHER_AGENT_ID = "conversation.someone_else"


def _add_light(hass: HomeAssistant, object_id: str, name: str, *, reverse: bool) -> str:
    """Register a light, set its state and expose it to Assist."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "light", "test", object_id, suggested_object_id=object_id, original_name=name
    )
    if reverse:
        registry.async_update_entity(entry.entity_id, labels={"reverse"})
    hass.states.async_set(entry.entity_id, "off", {"friendly_name": name})
    async_expose_entity(hass, conversation.DOMAIN, entry.entity_id, True)
    return entry.entity_id


@pytest.fixture
async def guarded_hass(hass: HomeAssistant) -> HomeAssistant:
    """Real Home Assistant with Assist, two lights and the guard for one agent."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    _add_light(hass, "lamp", "living room lamp", reverse=True)
    _add_light(hass, "heater", "bedroom heater", reverse=False)
    guard = async_get_local_guard(hass)
    guard.agent_ids.add(PEPA_AGENT_ID)
    assert async_guard_registered_handlers(hass, guard) > 0
    return hass


async def _local_attempt(
    hass: HomeAssistant, text: str, agent_id: str = PEPA_AGENT_ID
) -> intent.IntentResponse | None:
    """Run Home Assistant's prefer-local attempt the way the pipeline does."""
    user_input = conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id=agent_id,
    )
    with (
        chat_session.async_get_chat_session(hass, context=user_input.context) as session,
        conversation.async_get_chat_log(hass, session, user_input) as chat_log,
    ):
        return await conversation.async_handle_intents(hass, user_input, chat_log)


async def test_reversible_target_runs_locally(guarded_hass: HomeAssistant) -> None:
    """Every target carries reverse: Home Assistant acts as fast as before."""
    calls = async_mock_service(guarded_hass, "light", "turn_on")
    response = await _local_attempt(guarded_hass, "turn on the living room lamp")
    assert response is not None
    assert response.response_type is intent.IntentResponseType.ACTION_DONE
    assert [c.data["entity_id"] for c in calls] == [["light.lamp"]]


async def test_unlabelled_target_goes_to_the_agent(guarded_hass: HomeAssistant) -> None:
    """No reverse label: nothing is called and the request passes to the agent."""
    calls = async_mock_service(guarded_hass, "light", "turn_on")
    response = await _local_attempt(guarded_hass, "turn on the bedroom heater")
    assert response is None
    assert calls == []


async def test_turn_off_is_guarded_too(guarded_hass: HomeAssistant) -> None:
    """The same rule holds for turning off (the case found on ha-dev)."""
    calls = async_mock_service(guarded_hass, "light", "turn_off")
    response = await _local_attempt(guarded_hass, "turn off the bedroom heater")
    assert response is None
    assert calls == []


async def test_other_agents_are_not_touched(guarded_hass: HomeAssistant) -> None:
    """A request for any other agent behaves exactly as without the guard."""
    calls = async_mock_service(guarded_hass, "light", "turn_on")
    response = await _local_attempt(
        guarded_hass, "turn on the bedroom heater", agent_id=OTHER_AGENT_ID
    )
    assert response is not None
    assert [c.data["entity_id"] for c in calls] == [["light.heater"]]


async def test_wrapping_twice_changes_nothing(guarded_hass: HomeAssistant) -> None:
    """Re-running the wrap (a reload, a second entry) does not stack wrappers."""
    guard = async_get_local_guard(guarded_hass)
    before = len(guard.wrapped)
    assert async_guard_registered_handlers(guarded_hass, guard) == 0
    assert len(guard.wrapped) == before


async def test_all_reversible_rule(guarded_hass: HomeAssistant) -> None:
    """Whole request at once: mixed, unregistered and empty all fail closed."""
    lamp = guarded_hass.states.get("light.lamp")
    heater = guarded_hass.states.get("light.heater")
    guarded_hass.states.async_set("light.loose", "off")
    loose = guarded_hass.states.get("light.loose")

    assert async_all_reversible(guarded_hass, [lamp]) is True
    assert async_all_reversible(guarded_hass, [lamp, heater]) is False
    assert async_all_reversible(guarded_hass, [loose]) is False
    assert async_all_reversible(guarded_hass, []) is False
