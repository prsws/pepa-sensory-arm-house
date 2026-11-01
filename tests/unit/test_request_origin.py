"""Tests for request origin tracking (custom_components.pepa_sensory_arm_house.request_origin).

Each request records where it came from (a satellite, the conversation pipeline
without one, or the integration's own service) so that tools can later answer
through the same channel.
"""

import asyncio
from types import SimpleNamespace
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
from custom_components.pepa_sensory_arm_house.request_origin import (
    CHANNEL_CONVERSATION,
    CHANNEL_SATELLITE,
    CHANNEL_SERVICE,
    RequestOrigin,
    current_origin,
    request_origin,
)

SHOULD_EXPOSE = "homeassistant.components.homeassistant.exposed_entities.async_should_expose"


def _user_input(satellite_id=None, device_id=None, conversation_id="conv-1"):
    return SimpleNamespace(
        text="abre el portón",
        conversation_id=conversation_id,
        device_id=device_id,
        satellite_id=satellite_id,
        language="es",
        context=SimpleNamespace(user_id="user-1"),
    )


@pytest.fixture
def agent():
    from custom_components.pepa_sensory_arm_house.agent import HomeAgent
    from custom_components.pepa_sensory_arm_house.conversation_session import (
        ConversationSessionManager,
    )

    hass = MagicMock()
    hass.states.async_all = MagicMock(return_value=[])
    hass.data = {}
    config = {
        CONF_LLM_BASE_URL: "http://localhost:11434/v1",
        CONF_LLM_API_KEY: "",
        CONF_LLM_MODEL: "test-model",
        CONF_CONTEXT_MODE: CONTEXT_MODE_DIRECT,
        CONF_HISTORY_ENABLED: False,
        CONF_EMIT_EVENTS: False,
    }
    with patch(SHOULD_EXPOSE, return_value=False):
        agent = HomeAgent(hass, config, ConversationSessionManager(hass))
        agent._ensure_tools_registered()
    return agent


class TestRequestOriginModule:
    def test_none_outside_a_request(self):
        assert current_origin() is None

    def test_set_inside_block_and_cleared_after(self):
        origin = RequestOrigin(channel=CHANNEL_SERVICE)
        with request_origin(origin):
            assert current_origin() is origin
        assert current_origin() is None

    def test_cleared_after_an_exception(self):
        with pytest.raises(RuntimeError):
            with request_origin(RequestOrigin(channel=CHANNEL_SERVICE)):
                raise RuntimeError("boom")
        assert current_origin() is None

    def test_from_conversation_input_with_satellite(self):
        origin = RequestOrigin.from_conversation_input(
            _user_input(satellite_id="assist_satellite.sala", device_id="dev-sala")
        )
        assert origin == RequestOrigin(
            channel=CHANNEL_SATELLITE,
            satellite_id="assist_satellite.sala",
            device_id="dev-sala",
            conversation_id="conv-1",
            language="es",
        )

    def test_from_conversation_input_without_satellite(self):
        origin = RequestOrigin.from_conversation_input(_user_input())
        assert origin.channel == CHANNEL_CONVERSATION
        assert origin.satellite_id is None

    async def test_child_tasks_inherit_the_origin(self):
        """Tool calls run under asyncio.wait_for and gather; both must see it."""
        origin = RequestOrigin(channel=CHANNEL_SATELLITE, satellite_id="assist_satellite.sala")

        async def read():
            await asyncio.sleep(0)
            return current_origin()

        with request_origin(origin):
            assert await asyncio.wait_for(read(), timeout=1) is origin
            assert await asyncio.gather(read(), read()) == [origin, origin]


class TestAgentRecordsOrigin:
    async def test_satellite_request(self, agent):
        seen = {}

        async def capture(user_input):
            seen["origin"] = current_origin()
            return MagicMock()

        with (
            patch(SHOULD_EXPOSE, return_value=False),
            patch.object(agent, "_can_stream", return_value=False),
            patch.object(agent, "_async_process_synchronous", side_effect=capture),
        ):
            await agent.async_process(_user_input(satellite_id="assist_satellite.sala"))

        assert seen["origin"].channel == CHANNEL_SATELLITE
        assert seen["origin"].satellite_id == "assist_satellite.sala"
        assert current_origin() is None

    async def test_conversation_request_without_satellite(self, agent):
        seen = {}

        async def capture(user_input):
            seen["origin"] = current_origin()
            return MagicMock()

        with (
            patch(SHOULD_EXPOSE, return_value=False),
            patch.object(agent, "_can_stream", return_value=False),
            patch.object(agent, "_async_process_synchronous", side_effect=capture),
        ):
            await agent.async_process(_user_input())

        assert seen["origin"].channel == CHANNEL_CONVERSATION

    async def test_service_request(self, agent):
        seen = {}

        async def capture(*_args):
            seen["origin"] = current_origin()
            return "ok"

        with (
            patch(SHOULD_EXPOSE, return_value=False),
            patch.object(agent.session_manager, "update_activity", AsyncMock()),
            patch.object(agent, "_process_conversation", side_effect=capture),
        ):
            await agent.process_message("abre el portón", conversation_id="c1", device_id="dev-x")

        assert seen["origin"] == RequestOrigin(
            channel=CHANNEL_SERVICE, device_id="dev-x", conversation_id="c1"
        )
        assert seen["origin"].language is None  # no pipeline on the service channel
        assert current_origin() is None

    async def test_satellite_origin_survives_the_synchronous_path(self, agent):
        """async_process -> _async_process_synchronous -> process_message keeps the satellite."""
        seen = {}

        async def capture(*_args):
            seen["origin"] = current_origin()
            return "Abriendo el portón"

        with (
            patch(SHOULD_EXPOSE, return_value=False),
            patch.object(agent, "_can_stream", return_value=False),
            patch.object(agent.session_manager, "update_activity", AsyncMock()),
            patch.object(agent, "_process_conversation", side_effect=capture),
        ):
            await agent.async_process(_user_input(satellite_id="assist_satellite.sala"))

        assert seen["origin"].channel == CHANNEL_SATELLITE
        assert seen["origin"].satellite_id == "assist_satellite.sala"

    async def test_concurrent_requests_keep_their_own_origin(self, agent):
        seen = {}

        async def capture(user_input):
            await asyncio.sleep(0)  # let the other request run in between
            seen[user_input.satellite_id] = current_origin()
            return MagicMock()

        with (
            patch(SHOULD_EXPOSE, return_value=False),
            patch.object(agent, "_can_stream", return_value=False),
            patch.object(agent, "_async_process_synchronous", side_effect=capture),
        ):
            await asyncio.gather(
                agent.async_process(_user_input(satellite_id="assist_satellite.sala")),
                agent.async_process(_user_input(satellite_id="assist_satellite.cuarto")),
            )

        assert seen["assist_satellite.sala"].satellite_id == "assist_satellite.sala"
        assert seen["assist_satellite.cuarto"].satellite_id == "assist_satellite.cuarto"

    async def test_tools_see_the_origin(self, agent):
        """A tool executed through the real tool handler reads the request's origin."""
        seen = {}

        async def execute(**_kwargs):
            seen["origin"] = current_origin()
            return {"ok": True}

        probe = MagicMock()
        probe.name = "probe"
        probe.execute = execute
        agent.tool_handler.register_tool(probe)

        origin = RequestOrigin(channel=CHANNEL_SATELLITE, satellite_id="assist_satellite.sala")
        with request_origin(origin):
            await agent.tool_handler.execute_tool("probe", {})

        assert seen["origin"] is origin
