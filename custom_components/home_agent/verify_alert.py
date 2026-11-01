"""Loud verify failures: say it on the channel the request came from.

Pepa doctrine calls for loud failures. When a ``verify`` check fails:

- a Home Assistant notification is always created (it stays until dismissed);
- if the request came from a voice satellite that can make announcements and
  is available, the failure is also spoken on that satellite.

A request with no satellite (an automation through the integration's service,
or text typed in the app) gets the notification and the ``pepa_verify`` event.
A satellite that cannot announce, or is unavailable, gets the notification.

The message is a fixed sentence, not written by the language model, so it still
works when the model is what failed. Its language is the request's pipeline
language, or Home Assistant's own language when there was no pipeline. Spanish
and English are provided; any other language falls back to English.

Nothing waits: the announcement is started as a background task and the
notification is created synchronously in memory.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback

from .request_origin import CHANNEL_SATELLITE, RequestOrigin

_LOGGER = logging.getLogger(__name__)

ALERT_SATELLITE: Final = "satellite"
ALERT_NOTIFICATION: Final = "notification"

# assist_satellite's AssistSatelliteEntityFeature.ANNOUNCE (kept as a number so
# this module does not import the assist_satellite component).
SATELLITE_FEATURE_ANNOUNCE: Final = 1

_STATE_WORDS_ES: Final[dict[str, str]] = {
    "open": "abierto",
    "closed": "cerrado",
    "opening": "abriéndose",
    "closing": "cerrándose",
    "locked": "con seguro",
    "unlocked": "sin seguro",
    "locking": "poniendo el seguro",
    "unlocking": "quitando el seguro",
    "jammed": "atascado",
    "on": "encendido",
    "off": "apagado",
    "unavailable": "no disponible",
    "unknown": "desconocido",
}


def _language(hass: HomeAssistant, origin: RequestOrigin | None) -> str:
    language = (origin.language if origin else None) or hass.config.language or "en"
    return "es" if str(language).lower().startswith("es") else "en"


def _word(value: Any, language: str) -> str:
    text = "unknown" if value is None else str(value)
    if language == "es":
        if text.startswith("position "):
            return "en posición " + text.removeprefix("position ")
        return _STATE_WORDS_ES.get(text, text)
    return text


def failure_message(
    language: str, name: str, expected: str, observed: Any, reason: str | None, timeout_s: int
) -> tuple[str, str]:
    """Return (title, message) for a failed check, in Spanish or English."""
    if language == "es":
        title = "Pepa: no se pudo confirmar"
        if reason == "unavailable":
            message = f"Atención: {name} dejó de responder después de la orden."
        elif reason == "jammed":
            message = f"Atención: {name} está atascado."
        else:
            message = (
                f"Atención: {name} no confirmó la orden en {timeout_s} segundos. "
                f"Se esperaba «{_word(expected, language)}» "
                f"y el estado es «{_word(observed, language)}»."
            )
        return title, message

    title = "Pepa: could not confirm"
    if reason == "unavailable":
        message = f"Attention: {name} stopped responding after the command."
    elif reason == "jammed":
        message = f"Attention: {name} is jammed."
    else:
        message = (
            f"Attention: {name} did not confirm the command within {timeout_s} seconds. "
            f"Expected {_word(expected, language)}, but it is {_word(observed, language)}."
        )
    return title, message


def _satellite_can_announce(hass: HomeAssistant, satellite_id: str) -> bool:
    state = hass.states.get(satellite_id)
    if state is None or state.state == "unavailable":
        return False
    return bool(state.attributes.get("supported_features", 0) & SATELLITE_FEATURE_ANNOUNCE)


async def _announce(hass: HomeAssistant, satellite_id: str, message: str) -> None:
    try:
        await hass.services.async_call(
            "assist_satellite",
            "announce",
            {"message": message},
            target={"entity_id": satellite_id},
            blocking=False,
        )
    except Exception:  # noqa: BLE001 - a failed announcement must be logged, not lost
        _LOGGER.exception("verify: could not announce the failure on %s", satellite_id)


@callback
def async_alert_failure(
    hass: HomeAssistant,
    origin: RequestOrigin | None,
    entity_id: str,
    expected: str,
    observed: Any,
    reason: str | None,
    timeout_s: int,
) -> list[str]:
    """Make a failed check loud; return the alert channels used."""
    state = hass.states.get(entity_id)
    name = state.attributes.get("friendly_name", entity_id) if state else entity_id
    language = _language(hass, origin)
    title, message = failure_message(language, name, expected, observed, reason, timeout_s)

    alerts: list[str] = []
    satellite_id = origin.satellite_id if origin else None
    if origin and origin.channel == CHANNEL_SATELLITE and satellite_id:
        if _satellite_can_announce(hass, satellite_id):
            hass.async_create_task(_announce(hass, satellite_id, message))
            alerts.append(ALERT_SATELLITE)
        else:
            _LOGGER.warning(
                "verify: %s cannot announce right now; the failure goes to a notification",
                satellite_id,
            )

    persistent_notification.async_create(
        hass, message, title=title, notification_id=f"pepa_verify_{entity_id}"
    )
    alerts.append(ALERT_NOTIFICATION)
    return alerts
