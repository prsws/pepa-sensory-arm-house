"""Tests for asking before acting on what cannot be undone.

Covers the reusable ask-then-listen pattern (awaiting_reply), the confirmation
gate and its word-list decider (confirmation), the hold in ha_control, and the
agent turn that answers an awaited reply without the model.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import conversation as ha_conversation
from homeassistant.core import State
from homeassistant.helpers import intent

from custom_components.pepa_sensory_arm_house import awaiting_reply, confirmation
from custom_components.pepa_sensory_arm_house.awaiting_reply import (
    DROP_EXPIRED,
    DROP_REPLACED,
    DROP_STOPPED,
    AwaitingReplies,
    async_get_awaiting_replies,
    origin_key,
)
from custom_components.pepa_sensory_arm_house.confirmation import (
    CONFIRM_TIMEOUT_DEFAULT,
    DECISION_NO,
    DECISION_OTHER,
    DECISION_YES,
    EVENT_PEPA_CONFIRM,
    ConfirmationGate,
    ModelReplyDecider,
    WordListDecider,
    clamp_timeout,
    normalize,
)
from custom_components.pepa_sensory_arm_house.request_origin import (
    CHANNEL_CONVERSATION,
    CHANNEL_SATELLITE,
    CHANNEL_SERVICE,
    RequestOrigin,
    request_origin,
)
from custom_components.pepa_sensory_arm_house.tools.ha_control import HomeAssistantControlTool

GATE = "cover.porton"
SATELLITE = RequestOrigin(
    channel=CHANNEL_SATELLITE,
    satellite_id="assist_satellite.sala",
    device_id="dev-sala",
    conversation_id="conv-1",
    language="es",
)
OTHER_SATELLITE = RequestOrigin(
    channel=CHANNEL_SATELLITE, satellite_id="assist_satellite.cuarto", language="es"
)
TYPED = RequestOrigin(channel=CHANNEL_CONVERSATION, conversation_id="conv-2", language="en")
SERVICE = RequestOrigin(channel=CHANNEL_SERVICE, conversation_id="conv-3")


@pytest.fixture
def timers():
    """Capture async_call_later: the delay and the callback, without a clock."""
    hooks = SimpleNamespace(delays=[], actions=[], cancelled=0)

    def call_later(_hass, delay, action):
        hooks.delays.append(delay)
        hooks.actions.append(action)

        def cancel():
            hooks.cancelled += 1

        return cancel

    with patch.object(awaiting_reply, "async_call_later", side_effect=call_later):
        yield hooks


@pytest.fixture
def hass():
    hass = MagicMock()
    hass.data = {}
    hass.config.language = "es"

    async def run_in_executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = run_in_executor
    return hass


def _events(hass):
    return [
        call.args[1]
        for call in hass.bus.async_fire.call_args_list
        if call.args[0] == EVENT_PEPA_CONFIRM
    ]


async def _reply(text):
    return f"heard {text}"


class TestOriginKey:
    def test_satellite_by_entity_then_device(self):
        assert origin_key(SATELLITE) == "satellite:assist_satellite.sala"
        device_only = RequestOrigin(channel=CHANNEL_SATELLITE, device_id="dev-x")
        assert origin_key(device_only) == "satellite:dev-x"

    def test_conversation_by_id(self):
        assert origin_key(TYPED) == "conversation:conv-2"
        assert origin_key(RequestOrigin(channel=CHANNEL_CONVERSATION)) is None

    def test_nobody_to_answer(self):
        assert origin_key(SERVICE) is None
        assert origin_key(None) is None
        assert origin_key(RequestOrigin(channel=CHANNEL_SATELLITE)) is None


class TestAwaitingReplies:
    def test_service_channel_cannot_be_awaited(self, hass, timers):
        replies = AwaitingReplies(hass)
        assert replies.async_expect(SERVICE, "confirm", _reply, 10) is False
        assert timers.delays == []

    async def test_answer_goes_to_the_handler_once(self, hass, timers):
        replies = AwaitingReplies(hass)
        assert replies.async_expect(SATELLITE, "confirm", _reply, 10) is True
        assert timers.delays == [10]
        assert await replies.async_answer(SATELLITE, "sí") == "heard sí"
        assert timers.cancelled == 1
        assert await replies.async_answer(SATELLITE, "sí") is None

    async def test_only_the_same_origin_answers(self, hass, timers):
        replies = AwaitingReplies(hass)
        replies.async_expect(SATELLITE, "confirm", _reply, 10)
        assert await replies.async_answer(OTHER_SATELLITE, "sí") is None
        assert replies.is_awaiting(SATELLITE)

    def test_new_question_replaces_the_old_one(self, hass, timers):
        replies = AwaitingReplies(hass)
        dropped = []
        replies.async_expect(SATELLITE, "confirm", _reply, 10, dropped.append)
        replies.async_expect(SATELLITE, "confirm", _reply, 10)
        assert dropped == [DROP_REPLACED]
        assert timers.cancelled == 1

    def test_unanswered_question_expires(self, hass, timers):
        replies = AwaitingReplies(hass)
        dropped = []
        replies.async_expect(SATELLITE, "confirm", _reply, 10, dropped.append)
        timers.actions[0](None)
        assert dropped == [DROP_EXPIRED]
        assert not replies.is_awaiting(SATELLITE)

    def test_stale_timer_does_not_drop_a_newer_question(self, hass, timers):
        replies = AwaitingReplies(hass)
        dropped = []
        replies.async_expect(SATELLITE, "confirm", _reply, 10)
        replies.async_expect(SATELLITE, "confirm", _reply, 10, dropped.append)
        timers.actions[0](None)  # the first question's timer fires late
        assert dropped == []
        assert replies.is_awaiting(SATELLITE)

    def test_is_awaiting_since(self, hass, timers):
        replies = AwaitingReplies(hass)
        with patch.object(awaiting_reply.time, "monotonic", return_value=100.0):
            replies.async_expect(SATELLITE, "confirm", _reply, 10)
        assert replies.is_awaiting(SATELLITE, since=99.0)
        assert not replies.is_awaiting(SATELLITE, since=101.0)

    def test_stop_all(self, hass, timers):
        replies = AwaitingReplies(hass)
        dropped = []
        replies.async_expect(SATELLITE, "confirm", _reply, 10, dropped.append)
        replies.async_expect(TYPED, "confirm", _reply, 10, dropped.append)
        replies.async_stop_all()
        assert dropped == [DROP_STOPPED, DROP_STOPPED]

    def test_shared(self, hass):
        assert async_get_awaiting_replies(hass) is async_get_awaiting_replies(hass)


class TestWords:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("¡Sí!", "si"), ("Go ahead.", "go ahead"), ("Don't", "dont"), ("  DALE  ", "dale")],
    )
    def test_normalize(self, text, expected):
        assert normalize(text) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 1), (99, 60), (15, 15), ("20", 20), ("x", 10), (None, CONFIRM_TIMEOUT_DEFAULT)],
    )
    def test_clamp_timeout(self, value, expected):
        assert clamp_timeout(value) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Sí.", DECISION_YES),
            ("si", DECISION_YES),
            ("Dale", DECISION_YES),
            ("Yes please!", DECISION_YES),
            ("go ahead", DECISION_YES),
            ("No.", DECISION_NO),
            ("Cancela", DECISION_NO),
            ("never mind", DECISION_NO),
            ("sí, pero luego", DECISION_OTHER),
            ("prende la luz", DECISION_OTHER),
            ("", DECISION_OTHER),
        ],
    )
    async def test_word_list(self, hass, text, expected):
        assert await WordListDecider(hass).async_decide(text, "es") == expected

    def test_word_file_loads_as_text(self, hass):
        """Unquoted yes/no would load as booleans; every entry must stay text."""
        yes, no = WordListDecider(hass)._load()
        assert {"si", "yes", "dale", "go ahead"} <= yes
        assert {"no", "cancel", "dont"} <= no
        assert not yes & no

    async def test_model_decider_is_only_a_stub(self):
        with pytest.raises(NotImplementedError):
            await ModelReplyDecider().async_decide("sí", "es")


class TestConfirmationGate:
    def _hold(self, hass, origin, run=None, timeout_s=None):
        gate = ConfirmationGate(hass)
        run = run or AsyncMock(return_value={"success": True})
        result = gate.async_hold(
            origin=origin,
            entity_id=GATE,
            name="Portón",
            action="turn_on",
            run=run,
            timeout_s=timeout_s,
        )
        return gate, run, result

    def test_service_channel_is_refused(self, hass, timers):
        _, run, result = self._hold(hass, SERVICE)
        assert result["success"] is False
        assert result["confirmation_required"] is True
        assert "nobody can confirm" in result["message"]
        assert [e["outcome"] for e in _events(hass)] == ["refused"]
        run.assert_not_called()

    def test_held_and_the_model_is_told_to_ask(self, hass, timers):
        _, run, result = self._hold(hass, SATELLITE)
        assert result["confirmation_required"] is True
        assert "yes-or-no question" in result["message"]
        assert "Do not call ha_control again" in result["message"]
        assert timers.delays == [CONFIRM_TIMEOUT_DEFAULT]
        event = _events(hass)[0]
        assert event["outcome"] == "held"
        assert event["entity_id"] == GATE
        assert event["satellite_id"] == "assist_satellite.sala"
        assert event["timeout_s"] == CONFIRM_TIMEOUT_DEFAULT
        run.assert_not_called()

    def test_timeout_is_clamped(self, hass, timers):
        self._hold(hass, SATELLITE, timeout_s=300)
        assert timers.delays == [60]

    async def test_yes_runs_exactly_the_held_action(self, hass, timers):
        _, run, _ = self._hold(hass, SATELLITE)
        speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "Sí.")
        run.assert_awaited_once()
        assert speech == "Listo."
        assert [e["outcome"] for e in _events(hass)] == ["held", "confirmed"]

    async def test_yes_on_a_verify_entity_says_it_started(self, hass, timers):
        run = AsyncMock(return_value={"success": True, "verification": "pending"})
        self._hold(hass, SATELLITE, run=run)
        speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "dale")
        assert speech == "Dale, ya lo mandé. Te aviso si no termina."

    async def test_english_pipeline_answers_in_english(self, hass, timers):
        self._hold(hass, TYPED)
        assert await async_get_awaiting_replies(hass).async_answer(TYPED, "yes") == "Done."

    async def test_no_cancels_and_says_so(self, hass, timers):
        _, run, _ = self._hold(hass, SATELLITE)
        speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "no")
        run.assert_not_called()
        assert speech == "Ok, no lo hago."
        assert _events(hass)[-1]["outcome"] == "declined"

    async def test_anything_else_cancels_and_goes_to_the_model(self, hass, timers):
        _, run, _ = self._hold(hass, SATELLITE)
        speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "¿qué hora es?")
        run.assert_not_called()
        assert speech is None
        assert _events(hass)[-1]["outcome"] == "cancelled"
        assert not async_get_awaiting_replies(hass).is_awaiting(SATELLITE)

    async def test_failure_is_spoken(self, hass, timers):
        run = AsyncMock(side_effect=RuntimeError("boom"))
        self._hold(hass, SATELLITE, run=run)
        speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "sí")
        assert speech == "No pude hacerlo."
        assert _events(hass)[-1]["outcome"] == "failed"

    async def test_broken_decider_never_approves(self, hass, timers):
        gate, run, _ = self._hold(hass, SATELLITE)
        gate.decider = SimpleNamespace(async_decide=AsyncMock(side_effect=RuntimeError("x")))
        speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "sí")
        run.assert_not_called()
        assert speech is None
        assert _events(hass)[-1]["outcome"] == "cancelled"

    def test_expiry_fires_an_event(self, hass, timers):
        self._hold(hass, SATELLITE)
        timers.actions[0](None)
        assert _events(hass)[-1]["outcome"] == "expired"


class TestHaControlHolds:
    """ha_control holds actions on entities without the reverse label."""

    def _tool(self, hass, labels, exposed=None, confirm_timeout=None):
        states = {GATE: State(GATE, "closed", {"friendly_name": "Portón"})}
        hass.states.get.side_effect = states.get
        hass.services.async_call = AsyncMock()
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(labels=set(labels))
        tool = HomeAssistantControlTool(
            hass, exposed if exposed is not None else {GATE}, confirm_timeout=confirm_timeout
        )
        return tool, patch(
            "custom_components.pepa_sensory_arm_house.tools.ha_control.er.async_get",
            return_value=registry,
        )

    async def test_reversible_entity_acts_at_once(self, hass, timers):
        tool, registry_patch = self._tool(hass, ["reverse"])
        with registry_patch, request_origin(SATELLITE):
            result = await tool.execute(action="turn_on", entity_id=GATE)
        assert result["success"] is True
        hass.services.async_call.assert_awaited_once()

    async def test_unlabelled_entity_is_held_then_confirmed(self, hass, timers):
        tool, registry_patch = self._tool(hass, [], confirm_timeout=lambda: 30)
        with registry_patch:
            with request_origin(SATELLITE):
                result = await tool.execute(action="turn_on", entity_id=GATE)
            assert result["confirmation_required"] is True
            assert "Portón" in result["message"]
            assert timers.delays == [30]
            hass.services.async_call.assert_not_called()

            with request_origin(SATELLITE):
                speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "sí")
        assert speech == "Listo."
        hass.services.async_call.assert_awaited_once()
        assert hass.services.async_call.call_args.args[:2] == ("cover", "open_cover")

    async def test_unexposed_while_waiting_is_not_run(self, hass, timers):
        exposed = {GATE}
        tool, registry_patch = self._tool(hass, [], exposed=exposed)
        with registry_patch:
            with request_origin(SATELLITE):
                await tool.execute(action="turn_on", entity_id=GATE)
            exposed.clear()
            with request_origin(SATELLITE):
                speech = await async_get_awaiting_replies(hass).async_answer(SATELLITE, "sí")
        assert speech == "No pude hacerlo."
        hass.services.async_call.assert_not_called()

    async def test_service_channel_is_refused(self, hass, timers):
        tool, registry_patch = self._tool(hass, [])
        with registry_patch, request_origin(SERVICE):
            result = await tool.execute(action="turn_on", entity_id=GATE)
        assert result["success"] is False
        assert "nobody can confirm" in result["message"]
        hass.services.async_call.assert_not_called()

    def test_default_timeout(self, hass):
        assert confirmation.CONFIRM_TIMEOUT_DEFAULT == 10
        tool, _ = self._tool(hass, [])
        assert tool._confirm_timeout_s() == 10


SHOULD_EXPOSE = "homeassistant.components.homeassistant.exposed_entities.async_should_expose"


def _user_input(text, satellite_id="assist_satellite.sala"):
    return SimpleNamespace(
        text=text,
        conversation_id="conv-1",
        device_id="dev-sala",
        satellite_id=satellite_id,
        language="es",
        agent_id="agent-1",
        context=SimpleNamespace(user_id="user-1"),
    )


def _result(speech, keep_listening=False):
    response = intent.IntentResponse(language="es")
    response.async_set_speech(speech)
    return ha_conversation.ConversationResult(
        response=response, conversation_id="conv-1", continue_conversation=keep_listening
    )


@pytest.fixture
def agent(hass):
    from custom_components.pepa_sensory_arm_house.agent import HomeAgent
    from custom_components.pepa_sensory_arm_house.const import (
        CONF_CONTEXT_MODE,
        CONF_EMIT_EVENTS,
        CONF_HISTORY_ENABLED,
        CONF_LLM_API_KEY,
        CONF_LLM_BASE_URL,
        CONF_LLM_MODEL,
        CONTEXT_MODE_DIRECT,
    )
    from custom_components.pepa_sensory_arm_house.conversation_session import (
        ConversationSessionManager,
    )

    hass.states.async_all = MagicMock(return_value=[])
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


class TestAgentTurn:
    async def test_awaited_reply_is_answered_without_the_model(self, agent, hass, timers):
        async def handler(text):
            return f"respuesta a {text}"

        async_get_awaiting_replies(hass).async_expect(SATELLITE, "confirm", handler, 10)
        with patch.object(agent, "_async_process_turn", AsyncMock()) as model_turn:
            result = await agent.async_process(_user_input("sí"))
        model_turn.assert_not_called()
        assert result.response.speech["plain"]["speech"] == "respuesta a sí"
        assert result.continue_conversation is False

    async def test_reply_passed_on_goes_to_the_model(self, agent, hass, timers):
        async def handler(_text):
            return None

        async_get_awaiting_replies(hass).async_expect(SATELLITE, "confirm", handler, 10)
        with patch.object(
            agent, "_async_process_turn", AsyncMock(return_value=_result("ok"))
        ) as model_turn:
            result = await agent.async_process(_user_input("¿qué hora es?"))
        model_turn.assert_awaited_once()
        assert result.response.speech["plain"]["speech"] == "ok"

    async def test_turn_that_asks_keeps_the_microphone_open(self, agent, hass, timers):
        async def ask(user_input):
            async_get_awaiting_replies(hass).async_expect(SATELLITE, "confirm", _reply, 10)
            return _result("¿Abro el portón?")

        with patch.object(agent, "_async_process_turn", side_effect=ask):
            result = await agent.async_process(_user_input("abre el portón"))
        assert result.continue_conversation is True
        assert result.response.speech["plain"]["speech"] == "¿Abro el portón?"

    async def test_ordinary_turn_is_unchanged(self, agent, hass, timers):
        with patch.object(agent, "_async_process_turn", AsyncMock(return_value=_result("ok"))):
            result = await agent.async_process(_user_input("prende la luz"))
        assert result.continue_conversation is False

    async def test_question_from_another_satellite_does_not_keep_this_one_open(
        self, agent, hass, timers
    ):
        async_get_awaiting_replies(hass).async_expect(OTHER_SATELLITE, "confirm", _reply, 10)
        with patch.object(agent, "_async_process_turn", AsyncMock(return_value=_result("ok"))):
            result = await agent.async_process(_user_input("prende la luz"))
        assert result.continue_conversation is False
