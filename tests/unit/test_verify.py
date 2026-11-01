"""Tests for post-dispatch verification (custom_components.pepa_sensory_arm_house.verify)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import State

from custom_components.pepa_sensory_arm_house import verify
from custom_components.pepa_sensory_arm_house.request_origin import (
    CHANNEL_SATELLITE,
    RequestOrigin,
    request_origin,
)
from custom_components.pepa_sensory_arm_house.tools.ha_control import HomeAssistantControlTool
from custom_components.pepa_sensory_arm_house.verify import (
    EVENT_VERIFY,
    OUTCOME_CONFIRMED,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    OUTCOME_SUPERSEDED,
    OUTCOME_UNVERIFIABLE,
    VERIFY_TIMEOUT_SECONDS,
    VerifyManager,
    async_get_verify_manager,
    expected_outcome,
)

GATE = "cover.porton"
ORIGIN = RequestOrigin(
    channel=CHANNEL_SATELLITE,
    satellite_id="assist_satellite.sala",
    device_id="dev-sala",
    conversation_id="conv-1",
    language="es",
)


class TestExpectedOutcome:
    @pytest.mark.parametrize(
        ("domain", "service", "data", "previous", "expected"),
        [
            ("cover", "open_cover", {}, "closed", "open"),
            ("cover", "close_cover", {}, "open", "closed"),
            ("cover", "toggle", {}, "closed", "open"),
            ("cover", "toggle", {}, "opening", "closed"),
            ("lock", "lock", {}, "unlocked", "locked"),
            ("lock", "unlock", {}, "locked", "unlocked"),
            ("light", "turn_on", {}, "off", "on"),
            ("switch", "turn_off", {}, "on", "off"),
            ("fan", "toggle", {}, "on", "off"),
        ],
    )
    def test_implied_state(self, domain, service, data, previous, expected):
        expectation = expected_outcome(domain, service, data, previous)
        assert expectation is not None
        assert expectation.state == expected

    def test_cover_position_is_an_attribute(self):
        expectation = expected_outcome("cover", "set_cover_position", {"position": 50}, "closed")
        assert expectation.attribute == "current_position"
        assert expectation.value == 50
        assert expectation.is_met(State(GATE, "open", {"current_position": 50}))
        assert not expectation.is_met(State(GATE, "open", {"current_position": 40}))

    @pytest.mark.parametrize(
        ("domain", "service", "previous"),
        [
            ("cover", "stop_cover", "opening"),
            ("cover", "toggle", "unknown"),
            ("lock", "open", "locked"),
            ("light", "toggle", "unavailable"),
            ("climate", "set_temperature", "heat"),
        ],
    )
    def test_no_implied_state(self, domain, service, previous):
        assert expected_outcome(domain, service, {}, previous) is None


@pytest.fixture
def house():
    """A fake hass whose states and event subscriptions the test controls."""
    states = {GATE: State(GATE, "closed", {"friendly_name": "Portón"})}
    hass = MagicMock()
    hass.data = {}
    hass.states.get.side_effect = states.get
    hooks = SimpleNamespace(states=states, hass=hass, on_change=[], on_timeout=[], unsubscribed=[])

    def track(_hass, entity_ids, action):
        hooks.on_change.append(action)
        return lambda: hooks.unsubscribed.append("track")

    def call_later(_hass, delay, action):
        hooks.delay = delay
        hooks.on_timeout.append(action)
        return lambda: hooks.unsubscribed.append("timer")

    # The alert itself is tested in test_verify_alert.py; here we record the call.
    hooks.alerts = MagicMock(return_value=["notification"])

    with (
        patch.object(verify, "async_track_state_change_event", side_effect=track),
        patch.object(verify, "async_call_later", side_effect=call_later),
        patch.object(verify, "async_alert_failure", hooks.alerts),
    ):
        yield hooks


def _events(hooks):
    return [
        call.args[1]
        for call in hooks.hass.bus.async_fire.call_args_list
        if call.args[0] == EVENT_VERIFY
    ]


def _change(hooks, state, attributes=None):
    new = State(GATE, state, attributes or {})
    hooks.states[GATE] = new
    hooks.on_change[-1](SimpleNamespace(data={"new_state": new}))


class TestVerifyManager:
    def test_pending_subscribes_and_sets_the_deadline(self, house):
        manager = VerifyManager(house.hass)
        outcome = manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        assert outcome == OUTCOME_PENDING
        assert manager.pending_entity_ids == {GATE}
        assert house.delay == VERIFY_TIMEOUT_SECONDS
        assert _events(house) == []

    def test_in_progress_state_keeps_waiting(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        _change(house, "opening")
        assert manager.pending_entity_ids == {GATE}
        assert _events(house) == []

    def test_confirmed_when_expected_state_arrives(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        _change(house, "opening")
        _change(house, "open")
        [event] = _events(house)
        assert event["outcome"] == OUTCOME_CONFIRMED
        assert event["expected"] == "open"
        assert event["observed"] == "open"
        assert manager.pending_entity_ids == frozenset()
        assert sorted(house.unsubscribed) == ["timer", "track"]

    @pytest.mark.parametrize("bad_state", ["unavailable", "jammed"])
    def test_fails_early_on_bad_state(self, house, bad_state):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        _change(house, bad_state)
        [event] = _events(house)
        assert event["outcome"] == OUTCOME_FAILED
        assert event["reason"] == bad_state

    def test_fails_at_the_deadline(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        _change(house, "opening")
        house.on_timeout[-1](None)
        [event] = _events(house)
        assert event["outcome"] == OUTCOME_FAILED
        assert event["reason"] == "timeout"
        assert event["observed"] == "opening"
        assert manager.pending_entity_ids == frozenset()
        house.alerts.assert_called_once()
        assert house.alerts.call_args.args[1:6] == (ORIGIN, GATE, "open", "opening", "timeout")
        assert event["alerts"] == ["notification"]

    def test_only_failures_are_loud(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        _change(house, "open")  # confirmed
        manager.async_start(GATE, "stop_cover", {}, "open", ORIGIN)  # unverifiable
        house.alerts.assert_not_called()
        assert all(event["alerts"] == [] for event in _events(house))

    def test_deadline_confirms_if_state_arrived_unseen(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        house.states[GATE] = State(GATE, "open")
        house.on_timeout[-1](None)
        [event] = _events(house)
        assert event["outcome"] == OUTCOME_CONFIRMED

    def test_already_in_expected_state_confirms_at_once(self, house):
        house.states[GATE] = State(GATE, "open")
        manager = VerifyManager(house.hass)
        assert manager.async_start(GATE, "open_cover", {}, "open", ORIGIN) == OUTCOME_CONFIRMED
        [event] = _events(house)
        assert event["outcome"] == OUTCOME_CONFIRMED
        assert house.on_change == []

    def test_unverifiable_command(self, house):
        manager = VerifyManager(house.hass)
        assert (
            manager.async_start(GATE, "stop_cover", {}, "opening", ORIGIN) == OUTCOME_UNVERIFIABLE
        )
        [event] = _events(house)
        assert event["outcome"] == OUTCOME_UNVERIFIABLE
        assert event["expected"] is None
        assert house.on_change == []

    def test_newer_command_supersedes_and_old_callbacks_are_ignored(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        first_change, first_timeout = house.on_change[-1], house.on_timeout[-1]
        house.states[GATE] = State(GATE, "opening")  # changed its mind mid-way
        manager.async_start(GATE, "close_cover", {}, "opening", ORIGIN)
        [superseded] = _events(house)
        assert superseded["outcome"] == OUTCOME_SUPERSEDED
        assert superseded["command"] == "cover.open_cover"

        # Late callbacks from the first check change nothing.
        first_change(SimpleNamespace(data={"new_state": State(GATE, "open")}))
        first_timeout(None)
        assert len(_events(house)) == 1
        assert manager.pending_entity_ids == {GATE}

    def test_event_carries_origin_language_and_duration(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        house.on_timeout[-1](None)
        [event] = _events(house)
        assert event["entity_id"] == GATE
        assert event["command"] == "cover.open_cover"
        assert event["origin"]["channel"] == CHANNEL_SATELLITE
        assert event["origin"]["satellite_id"] == "assist_satellite.sala"
        assert event["origin"]["language"] == "es"
        assert isinstance(event["duration_s"], float)

    def test_stop_all_cancels_without_outcomes(self, house):
        manager = VerifyManager(house.hass)
        manager.async_start(GATE, "open_cover", {}, "closed", ORIGIN)
        manager.async_stop_all()
        assert manager.pending_entity_ids == frozenset()
        assert sorted(house.unsubscribed) == ["timer", "track"]
        assert _events(house) == []

    def test_manager_is_shared(self):
        hass = MagicMock()
        hass.data = {}
        assert async_get_verify_manager(hass) is async_get_verify_manager(hass)


class TestHaControlStartsVerify:
    """ha_control returns at once and tells the model the result is pending."""

    def _tool(self, house, labels):
        tool = HomeAssistantControlTool(house.hass)
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(labels=set(labels))
        house.hass.states.get.side_effect = house.states.get
        house.hass.services.async_call = MagicMock(return_value=_done())
        return tool, patch(
            "custom_components.pepa_sensory_arm_house.tools.ha_control.er.async_get",
            return_value=registry,
        )

    async def test_verify_entity_answers_pending_without_state(self, house):
        tool, registry_patch = self._tool(house, ["verify", "reverse"])
        with registry_patch, request_origin(ORIGIN):
            result = await tool.execute(action="turn_on", entity_id=GATE)
        assert result["success"] is True
        assert result["verification"] == OUTCOME_PENDING
        assert "new_state" not in result
        assert "not that it is done" in result["message"]
        manager = async_get_verify_manager(house.hass)
        assert manager.pending_entity_ids == {GATE}
        assert manager._checks[GATE].origin == ORIGIN
        assert manager._checks[GATE].command == "cover.open_cover"

    async def test_other_entities_are_unchanged(self, house):
        tool, registry_patch = self._tool(house, ["reverse"])
        with registry_patch:
            result = await tool.execute(action="turn_on", entity_id=GATE)
        assert "verification" not in result
        assert result["new_state"] == "closed"
        assert async_get_verify_manager(house.hass).pending_entity_ids == frozenset()


async def _done():
    return None
