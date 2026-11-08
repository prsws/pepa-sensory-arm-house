"""Local handling guard against a real Home Assistant: device actions."""

from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_mock_service

from custom_components.pepa_sensory_arm_house.const import DOMAIN
from custom_components.pepa_sensory_arm_house.local_guard import (
    ISSUE_NOT_GUARDING,
    READ_INTENTS,
    async_all_reversible,
    async_check,
    async_disable,
    async_enable,
    async_get_local_guard,
    async_guard_registered_handlers,
    async_report,
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


async def test_questions_answer_locally(guarded_hass: HomeAssistant) -> None:
    """A read intent through a Pepa pipeline is answered by Home Assistant."""
    response = await _local_attempt(guarded_hass, "is the bedroom heater on?")
    assert response is not None
    assert response.response_type is intent.IntentResponseType.QUERY_ANSWER


async def test_other_intents_go_to_the_agent(hass: HomeAssistant) -> None:
    """A non-read intent that is not a device action is refused for Pepa only."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    assert await async_setup_component(hass, "shopping_list", {})
    guard = async_get_local_guard(hass)
    guard.agent_ids.add(PEPA_AGENT_ID)
    async_guard_registered_handlers(hass, guard)

    assert await _local_attempt(hass, "add milk to my shopping list") is None
    # For another agent Home Assistant handles it itself. (Without a shopping
    # list config entry its own handler then fails, which does not matter here:
    # what matters is that the request was not handed on.)
    other = await _local_attempt(hass, "add eggs to my shopping list", agent_id=OTHER_AGENT_ID)
    assert other is not None
    assert other.error_code is not intent.IntentResponseErrorCode.NO_VALID_TARGETS


async def test_unknown_intent_is_refused_for_pepa_only(guarded_hass: HomeAssistant) -> None:
    """An intent the guard has never heard of fails closed (for Pepa only)."""

    class NewIntent(intent.IntentHandler):
        intent_type = "HassSomethingNew"

        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            return intent_obj.create_response()

    intent.async_register(guarded_hass, NewIntent())
    async_guard_registered_handlers(guarded_hass, async_get_local_guard(guarded_hass))

    with pytest.raises(intent.MatchFailedError):
        await intent.async_handle(
            guarded_hass, "test", "HassSomethingNew", conversation_agent_id=PEPA_AGENT_ID
        )
    response = await intent.async_handle(
        guarded_hass, "test", "HassSomethingNew", conversation_agent_id=OTHER_AGENT_ID
    )
    assert response is not None


def test_read_list_names_real_intents() -> None:
    """Every read intent name exists in this Home Assistant version."""
    from pathlib import Path

    import homeassistant

    root = Path(homeassistant.__file__).parent
    sources = (root / "helpers/intent.py").read_text() + (
        root / "components/weather/const.py"
    ).read_text()
    for name in READ_INTENTS:
        assert f'"{name}"' in sources, name


async def _assist_hass(hass: HomeAssistant) -> HomeAssistant:
    """Assist and two lights, without the guard."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    _add_light(hass, "lamp", "living room lamp", reverse=True)
    _add_light(hass, "heater", "bedroom heater", reverse=False)
    return hass


async def test_enable_and_disable_round_trip(hass: HomeAssistant) -> None:
    """On with the first entry, off and fully restored after the last one."""
    await _assist_hass(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    guard = async_get_local_guard(hass)

    async_enable(hass, PEPA_AGENT_ID)
    async_enable(hass, "second_entry")
    assert guard.active
    assert await _local_attempt(hass, "turn on the bedroom heater") is None

    async_disable(hass, "second_entry")
    assert guard.active
    assert await _local_attempt(hass, "turn on the bedroom heater") is None

    async_disable(hass, PEPA_AGENT_ID)
    assert not guard.active
    assert guard.wrapped == {}
    assert guard.unsub_component_loaded is None
    for handler in intent.async_get(hass):
        assert "async_handle_states" not in vars(handler)
        assert "async_handle" not in vars(handler)
    # Restored: Home Assistant acts by itself again, as without the guard.
    assert await _local_attempt(hass, "turn on the bedroom heater") is not None
    assert len(calls) == 1


async def test_handlers_added_later_are_wrapped(hass: HomeAssistant) -> None:
    """A handler registered after the guard started is wrapped when its
    integration finishes loading."""
    await _assist_hass(hass)
    async_enable(hass, PEPA_AGENT_ID)

    class LateIntent(intent.IntentHandler):
        intent_type = "HassLateArrival"

        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            return intent_obj.create_response()

    late = LateIntent()
    intent.async_register(hass, late)
    assert "async_handle" not in vars(late)

    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "late_integration"})
    await hass.async_block_till_done()

    assert "async_handle" in vars(late)
    with pytest.raises(intent.MatchFailedError):
        await intent.async_handle(
            hass, "test", "HassLateArrival", conversation_agent_id=PEPA_AGENT_ID
        )
    async_disable(hass, PEPA_AGENT_ID)


async def _guarded_and_started(hass: HomeAssistant) -> None:
    await _assist_hass(hass)
    async_enable(hass, PEPA_AGENT_ID)
    await hass.async_block_till_done()


async def test_startup_check_finds_nothing_wrong(hass: HomeAssistant) -> None:
    """With every handler wrapped and the hook points present, no repair notice."""
    await _guarded_and_started(hass)
    assert async_check(hass, async_get_local_guard(hass)) == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NOT_GUARDING) is None
    async_disable(hass, PEPA_AGENT_ID)


async def test_unwrapped_handler_raises_a_repair_notice(hass: HomeAssistant) -> None:
    """A handler the guard missed is reported, then cleared once it is wrapped."""
    await _guarded_and_started(hass)
    guard = async_get_local_guard(hass)

    class Sneaky(intent.IntentHandler):
        intent_type = "HassSneaky"

        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            return intent_obj.create_response()

    intent.async_register(hass, Sneaky())
    assert async_report(hass, guard) is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NOT_GUARDING)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert "HassSneaky" in issue.translation_placeholders["problems"]

    # Another integration loads: the straggler is wrapped and the notice goes away.
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "late_integration"})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NOT_GUARDING) is None
    async_disable(hass, PEPA_AGENT_ID)


async def test_missing_hook_point_raises_a_repair_notice(hass: HomeAssistant) -> None:
    """If Home Assistant stops handing refused requests on, say so loudly."""
    await _guarded_and_started(hass)
    with patch(
        "homeassistant.components.conversation.default_agent._MATCH_ERROR_CODES",
        frozenset(),
    ):
        assert async_report(hass, async_get_local_guard(hass)) is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NOT_GUARDING)
    assert "handed to the agent" in issue.translation_placeholders["problems"]
    async_disable(hass, PEPA_AGENT_ID)


async def test_switching_off_clears_the_notice(hass: HomeAssistant) -> None:
    """With the last entry gone there is nothing to guard and nothing to report."""
    await _guarded_and_started(hass)
    with patch(
        "homeassistant.components.conversation.default_agent._MATCH_ERROR_CODES",
        frozenset(),
    ):
        async_report(hass, async_get_local_guard(hass))
    async_disable(hass, PEPA_AGENT_ID)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NOT_GUARDING) is None
