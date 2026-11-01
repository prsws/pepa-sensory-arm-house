"""Cold-boot tests for the exposed-entity allow-list refresh.

On a fresh boot the first message can arrive before late integrations (Zigbee,
MQTT discovery) have published their entities. The allow-list the tools use must
pick those entities up on the next turn, through both ways a message reaches the
agent: the conversation path (async_process) and the integration's own service
(process_message).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pepa_sensory_arm_house.const import (
    CONF_CONTEXT_MODE,
    CONF_EMIT_EVENTS,
    CONF_HISTORY_ENABLED,
    CONF_LLM_API_KEY,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONTEXT_MODE_DIRECT,
)
from custom_components.pepa_sensory_arm_house.exceptions import PermissionDenied

# _refresh_exposed_entities imports async_should_expose inside the function, so it
# must be patched where it is defined, not in the agent module.
SHOULD_EXPOSE = "homeassistant.components.homeassistant.exposed_entities.async_should_expose"

EARLY_ENTITY = "light.kitchen"
LATE_ENTITY = "fan.bedroom"  # appears only after the first turn (late discovery)


def _state(entity_id: str) -> MagicMock:
    state = MagicMock()
    state.entity_id = entity_id
    return state


@pytest.fixture
def mock_hass():
    """Home Assistant with only the early entity present at first."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.async_all = MagicMock(return_value=[_state(EARLY_ENTITY)])
    hass.data = {}
    hass.bus = MagicMock()
    hass.config = MagicMock()
    hass.config.location_name = "Test Home"
    return hass


@pytest.fixture
def agent(mock_hass):
    """An agent with tools registered while only the early entity exists."""
    from custom_components.pepa_sensory_arm_house.agent import HomeAgent
    from custom_components.pepa_sensory_arm_house.conversation_session import (
        ConversationSessionManager,
    )

    config = {
        CONF_LLM_BASE_URL: "http://localhost:11434/v1",
        CONF_LLM_API_KEY: "",
        CONF_LLM_MODEL: "test-model",
        CONF_CONTEXT_MODE: CONTEXT_MODE_DIRECT,
        CONF_HISTORY_ENABLED: False,
        CONF_EMIT_EVENTS: False,
    }
    with patch(SHOULD_EXPOSE, return_value=True):
        agent = HomeAgent(mock_hass, config, ConversationSessionManager(mock_hass))
        agent._ensure_tools_registered()
    return agent


def _late_entity_appears(hass) -> None:
    hass.states.async_all.return_value = [_state(EARLY_ENTITY), _state(LATE_ENTITY)]


def _assert_tools_allow(agent, entity_id: str) -> None:
    for name in ("ha_control", "ha_query"):
        agent.tool_handler.tools[name]._validate_entity_access(entity_id)


def _assert_tools_deny(agent, entity_id: str) -> None:
    for name in ("ha_control", "ha_query"):
        with pytest.raises(PermissionDenied):
            agent.tool_handler.tools[name]._validate_entity_access(entity_id)


def test_cold_boot_snapshot_excludes_late_entity(agent):
    """Before any refresh, the late entity is not in the allow-list."""
    _assert_tools_allow(agent, EARLY_ENTITY)
    _assert_tools_deny(agent, LATE_ENTITY)


@pytest.mark.asyncio
async def test_conversation_path_picks_up_late_entity(agent, mock_hass):
    """A turn through async_process makes the late entity reachable."""
    _late_entity_appears(mock_hass)
    result = MagicMock()

    with (
        patch(SHOULD_EXPOSE, return_value=True),
        patch.object(agent, "_can_stream", return_value=False),
        patch.object(agent, "_async_process_synchronous", AsyncMock(return_value=result)),
    ):
        assert await agent.async_process(MagicMock()) is result

    _assert_tools_allow(agent, LATE_ENTITY)


@pytest.mark.asyncio
async def test_service_path_picks_up_late_entity(agent, mock_hass):
    """A call through process_message (the integration's own service) does too."""
    _late_entity_appears(mock_hass)

    with (
        patch(SHOULD_EXPOSE, return_value=True),
        patch.object(agent.session_manager, "update_activity", AsyncMock()),
        patch.object(agent, "_process_conversation", AsyncMock(return_value="ok")),
    ):
        await agent.process_message("turn on the fan", conversation_id="c1")

    _assert_tools_allow(agent, LATE_ENTITY)


@pytest.mark.asyncio
async def test_service_path_drops_unexposed_entity(agent, mock_hass):
    """Exposure removed in Home Assistant is honoured on the next turn."""
    with (
        patch(SHOULD_EXPOSE, return_value=False),
        patch.object(agent.session_manager, "update_activity", AsyncMock()),
        patch.object(agent, "_process_conversation", AsyncMock(return_value="ok")),
    ):
        await agent.process_message("turn on the light", conversation_id="c1")

    _assert_tools_deny(agent, EARLY_ENTITY)
