"""Tests for loud verify failures (custom_components.pepa_sensory_arm_house.verify_alert)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import State

from custom_components.pepa_sensory_arm_house import verify_alert
from custom_components.pepa_sensory_arm_house.request_origin import (
    CHANNEL_CONVERSATION,
    CHANNEL_SATELLITE,
    CHANNEL_SERVICE,
    RequestOrigin,
)
from custom_components.pepa_sensory_arm_house.verify_alert import (
    ALERT_NOTIFICATION,
    ALERT_SATELLITE,
    async_alert_failure,
    failure_message,
)

GATE = "cover.porton"
SATELLITE = "assist_satellite.sala"


class TestFailureMessage:
    def test_spanish_timeout(self):
        title, message = failure_message("es", "Portón", "open", "closed", "timeout", 60)
        assert title == "Pepa: no se pudo confirmar"
        assert message == (
            "Atención: Portón no confirmó la orden en 60 segundos. "
            "Se esperaba «abierto» y el estado es «cerrado»."
        )

    def test_english_timeout(self):
        title, message = failure_message("en", "Carport gate", "open", "opening", "timeout", 60)
        assert title == "Pepa: could not confirm"
        assert message == (
            "Attention: Carport gate did not confirm the command within 60 seconds. "
            "Expected open, but it is opening."
        )

    @pytest.mark.parametrize(
        ("language", "reason", "expected"),
        [
            ("es", "unavailable", "Atención: Portón dejó de responder después de la orden."),
            ("es", "jammed", "Atención: Portón está atascado."),
            ("en", "unavailable", "Attention: Portón stopped responding after the command."),
            ("en", "jammed", "Attention: Portón is jammed."),
        ],
    )
    def test_early_failures(self, language, reason, expected):
        assert failure_message(language, "Portón", "open", reason, reason, 60)[1] == expected

    def test_spanish_position_and_unknown(self):
        message = failure_message("es", "Persiana", "position 50", None, "timeout", 60)[1]
        assert "«en posición 50»" in message
        assert "«desconocido»" in message


@pytest.fixture
def hass():
    states = {GATE: State(GATE, "closed", {"friendly_name": "Portón"})}
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    hass.config.language = "en"
    hass.services.async_call = AsyncMock()
    hass._states = states
    return hass


def _satellite(hass, features=1, state="idle"):
    hass._states[SATELLITE] = State(SATELLITE, state, {"supported_features": features})


def _origin(channel=CHANNEL_SATELLITE, language="es"):
    return RequestOrigin(
        channel=channel,
        satellite_id=SATELLITE if channel == CHANNEL_SATELLITE else None,
        language=language,
    )


@pytest.fixture
def notify():
    with patch.object(verify_alert.persistent_notification, "async_create") as create:
        yield create


class TestAlertFailure:
    async def test_satellite_announces_and_notifies(self, hass, notify):
        _satellite(hass)
        alerts = async_alert_failure(hass, _origin(), GATE, "open", "closed", "timeout", 60)
        assert alerts == [ALERT_SATELLITE, ALERT_NOTIFICATION]

        # The announcement runs as a background task; run it here.
        [coro] = [call.args[0] for call in hass.async_create_task.call_args_list]
        await coro
        hass.services.async_call.assert_awaited_once()
        args, kwargs = hass.services.async_call.call_args
        assert args[:2] == ("assist_satellite", "announce")
        assert "Portón no confirmó la orden" in args[2]["message"]
        assert kwargs["target"] == {"entity_id": SATELLITE}
        assert kwargs["blocking"] is False

        notify.assert_called_once()
        assert notify.call_args.kwargs["title"] == "Pepa: no se pudo confirmar"
        assert notify.call_args.kwargs["notification_id"] == f"pepa_verify_{GATE}"

    @pytest.mark.parametrize(("features", "state"), [(0, "idle"), (1, "unavailable")])
    async def test_satellite_that_cannot_announce_gets_notification(
        self, hass, notify, features, state
    ):
        _satellite(hass, features=features, state=state)
        alerts = async_alert_failure(hass, _origin(), GATE, "open", "closed", "timeout", 60)
        assert alerts == [ALERT_NOTIFICATION]
        hass.async_create_task.assert_not_called()
        notify.assert_called_once()

    @pytest.mark.parametrize("channel", [CHANNEL_SERVICE, CHANNEL_CONVERSATION])
    async def test_no_satellite_gets_notification(self, hass, notify, channel):
        alerts = async_alert_failure(
            hass, _origin(channel, language=None), GATE, "open", "closed", "timeout", 60
        )
        assert alerts == [ALERT_NOTIFICATION]
        hass.async_create_task.assert_not_called()
        # No pipeline: Home Assistant's own language (English here).
        assert notify.call_args.args[1].startswith("Attention: Portón")

    async def test_announcement_failure_is_logged_not_raised(self, hass, notify, caplog):
        _satellite(hass)
        hass.services.async_call.side_effect = RuntimeError("satellite gone")
        async_alert_failure(hass, _origin(), GATE, "open", "closed", "timeout", 60)
        [coro] = [call.args[0] for call in hass.async_create_task.call_args_list]
        await coro
        assert "could not announce the failure" in caplog.text
