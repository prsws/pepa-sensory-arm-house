"""Toggle rule against a real Home Assistant: run a toggle only when asked."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import async_mock_service

from custom_components.pepa_sensory_arm_house.action_words import async_get_action_words
from custom_components.pepa_sensory_arm_house.exceptions import ValidationError
from custom_components.pepa_sensory_arm_house.request_origin import (
    CHANNEL_CONVERSATION,
    CHANNEL_SERVICE,
    RequestOrigin,
    request_origin,
)
from custom_components.pepa_sensory_arm_house.tools.ha_control import HomeAssistantControlTool


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("turn on the fan", "turn_on"),
        ("Turn ON the living room fan, please", "turn_on"),
        ("enciende el abanico", "turn_on"),
        ("prende la luz", "turn_on"),
        ("open the garage door", "turn_on"),
        ("turn off the fan", "turn_off"),
        ("apaga el abanico de la sala", "turn_off"),
        ("cierra el portón", "turn_off"),
        ("toggle the lamp", "toggle"),
        ("alterna la lámpara", "toggle"),
        ("the living room fan", None),
        ("el de la sala", None),
        ("turn on the lamp and turn off the fan", None),
        ("upon reflection, the onion", None),
        (None, "toggle"),
    ],
)
async def test_resolve_toggle(hass: HomeAssistant, text, expected) -> None:
    """The person's words decide; no words at all (service channel) keep the toggle."""
    assert await async_get_action_words(hass).async_resolve_toggle(text) == expected


@pytest.fixture
async def fan(hass: HomeAssistant) -> str:
    """A reversible fan that is on (so a toggle would turn it off)."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "fan", "test", "sala", suggested_object_id="sala", original_name="Abanico Sala"
    )
    registry.async_update_entity(entry.entity_id, labels={"reverse"})
    hass.states.async_set(entry.entity_id, "on", {"friendly_name": "Abanico Sala"})
    return entry.entity_id


def _conversation(text: str) -> RequestOrigin:
    return RequestOrigin(channel=CHANNEL_CONVERSATION, conversation_id="c1", text=text)


async def test_turn_on_sent_as_toggle_runs_as_turn_on(hass: HomeAssistant, fan: str) -> None:
    """The ha-dev case: 'turn on the fan' sent as toggle must not turn a running fan off."""
    calls = {s: async_mock_service(hass, "fan", s) for s in ("toggle", "turn_on", "turn_off")}
    tool = HomeAssistantControlTool(hass)
    with request_origin(_conversation("turn on the living room fan")):
        await tool.execute(action="toggle", entity_id=fan)
    assert [len(calls[s]) for s in ("toggle", "turn_on", "turn_off")] == [0, 1, 0]


async def test_toggle_without_direction_is_refused(hass: HomeAssistant, fan: str) -> None:
    """'el de la sala' says which device, not which way: no toggle, nothing called."""
    calls = async_mock_service(hass, "fan", "toggle")
    tool = HomeAssistantControlTool(hass)
    with request_origin(_conversation("el de la sala")), pytest.raises(ValidationError):
        await tool.execute(action="toggle", entity_id=fan)
    assert calls == []


async def test_toggle_asked_for_still_toggles(hass: HomeAssistant, fan: str) -> None:
    """A person who says toggle gets a toggle."""
    calls = async_mock_service(hass, "fan", "toggle")
    tool = HomeAssistantControlTool(hass)
    with request_origin(_conversation("toggle the living room fan")):
        await tool.execute(action="toggle", entity_id=fan)
    assert len(calls) == 1


async def test_service_channel_keeps_its_toggle(hass: HomeAssistant, fan: str) -> None:
    """An automation calling the service with toggle means it."""
    calls = async_mock_service(hass, "fan", "toggle")
    tool = HomeAssistantControlTool(hass)
    with request_origin(RequestOrigin(channel=CHANNEL_SERVICE)):
        await tool.execute(action="toggle", entity_id=fan)
    assert len(calls) == 1
