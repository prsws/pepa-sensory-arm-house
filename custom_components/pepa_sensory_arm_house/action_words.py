"""Toggle rule: run a toggle only when the person asked for one.

``toggle`` flips whatever a device is doing now. When the model sends a toggle
for "turn on the fan", a fan that is already running is turned off. This module
checks the words of the request the action came from (``RequestOrigin.text``)
against the fixed lists in ``action_words.yaml`` and decides, in code:

- said on, not off  -> ``turn_on``
- said off, not on  -> ``turn_off``
- said toggle       -> ``toggle``
- anything else     -> refused (``None``); the model must use turn_on or
  turn_off as the person asked.

Requests with no text (the integration's service, used by automations and
scripts) keep their toggle: an automation that asks for a toggle means it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from homeassistant.core import HomeAssistant

from .confirmation import normalize
from .const import ACTION_TOGGLE, ACTION_TURN_OFF, ACTION_TURN_ON

ACTION_WORDS_FILE: Final = Path(__file__).parent / "action_words.yaml"
DATA_ACTION_WORDS: Final = "pepa_action_words"


class ActionWords:
    """The on, off and toggle word lists, read on first use, off the event loop."""

    def __init__(self, hass: HomeAssistant, path: Path = ACTION_WORDS_FILE) -> None:
        """Initialize."""
        self._hass = hass
        self._path = path
        self._lists: dict[str, frozenset[str]] | None = None

    def _load(self) -> dict[str, frozenset[str]]:
        with open(self._path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        def _phrases(section: Any) -> frozenset[str]:
            phrases: set[str] = set()
            if isinstance(section, dict):
                for words in section.values():
                    for word in words or ():
                        if normalized := normalize(str(word)):
                            phrases.add(normalized)
            return frozenset(phrases)

        return {
            key: _phrases(data.get(key)) for key in (ACTION_TURN_ON, ACTION_TURN_OFF, ACTION_TOGGLE)
        }

    async def async_resolve_toggle(self, text: str | None) -> str | None:
        """Return the action a toggle should run as, or None to refuse it."""
        if text is None:
            return ACTION_TOGGLE
        if self._lists is None:
            self._lists = await self._hass.async_add_executor_job(self._load)
        request = f" {normalize(text)} "

        def said(key: str) -> bool:
            return any(f" {phrase} " in request for phrase in self._lists[key])

        if said(ACTION_TOGGLE):
            return ACTION_TOGGLE
        on, off = said(ACTION_TURN_ON), said(ACTION_TURN_OFF)
        if on and not off:
            return ACTION_TURN_ON
        if off and not on:
            return ACTION_TURN_OFF
        return None


def async_get_action_words(hass: HomeAssistant) -> ActionWords:
    """Return the shared word lists, creating them on first use."""
    if (words := hass.data.get(DATA_ACTION_WORDS)) is None:
        words = hass.data[DATA_ACTION_WORDS] = ActionWords(hass)
    return words
