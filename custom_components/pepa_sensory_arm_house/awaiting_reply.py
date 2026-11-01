"""Ask, then listen: treat the user's next words as the answer to a question.

Some features need to ask the user something and take the next utterance from
the same place as the answer. Confirming an action that cannot be undone is the
first; others will follow. This module is the shared pattern:

- a feature registers what it is waiting for with ``async_expect``: the origin
  it asked (see request_origin), a handler for the answer and a time limit;
- at the start of the next turn from that origin, the agent hands the utterance
  to the handler before the language model runs (``async_answer``). The handler
  either answers itself (returns the sentence to speak, and the model never sees
  the turn) or returns None to let the turn go to the model as usual;
- while something is awaited, the agent keeps the satellite's microphone open
  for the reply, even when follow-up listening is off, so the user does not have
  to say the wake word again.

One question per origin. A new question replaces the previous one, an answer
consumes it, and an unanswered one expires. Whatever ends a question without an
answer is reported to the feature through ``on_drop`` with the reason.

Only a person can answer: the satellite and conversation channels qualify. The
service channel (automations, scripts, other arms through the integration's
service) has nobody to answer, so nothing can be awaited there and
``async_expect`` returns False.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .request_origin import CHANNEL_CONVERSATION, CHANNEL_SATELLITE, RequestOrigin

_LOGGER = logging.getLogger(__name__)

DATA_AWAITING_REPLIES: Final = "pepa_awaiting_replies"

# Why a question ended without an answer.
DROP_EXPIRED: Final = "expired"
DROP_REPLACED: Final = "replaced"
DROP_STOPPED: Final = "stopped"

# Handles the answer: returns the sentence to speak, or None to pass the turn on
# to the language model.
ReplyHandler = Callable[[str], Awaitable[str | None]]
# Told why a question ended without an answer.
DropHandler = Callable[[str], None]


def origin_key(origin: RequestOrigin | None) -> str | None:
    """Return the key that identifies where a reply must come from, or None.

    A satellite is identified by its entity (or device); a conversation without
    a satellite by its conversation id. The service channel has no key: there is
    nobody there to answer.
    """
    if origin is None:
        return None
    if origin.channel == CHANNEL_SATELLITE:
        target = origin.satellite_id or origin.device_id
        return f"satellite:{target}" if target else None
    if origin.channel == CHANNEL_CONVERSATION and origin.conversation_id:
        return f"conversation:{origin.conversation_id}"
    return None


@dataclass
class Awaited:
    """One question waiting for its answer."""

    kind: str
    handler: ReplyHandler
    on_drop: DropHandler | None
    created: float
    cancel_timer: CALLBACK_TYPE | None = None


class AwaitingReplies:
    """The questions waiting for an answer, one per origin."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize with no questions."""
        self._hass = hass
        self._awaited: dict[str, Awaited] = {}

    @callback
    def async_expect(
        self,
        origin: RequestOrigin | None,
        kind: str,
        handler: ReplyHandler,
        timeout_s: float,
        on_drop: DropHandler | None = None,
    ) -> bool:
        """Wait for the next words from ``origin``. Returns False if nobody can answer."""
        key = origin_key(origin)
        if key is None:
            return False
        self._drop(key, DROP_REPLACED)

        awaited = Awaited(kind=kind, handler=handler, on_drop=on_drop, created=time.monotonic())

        @callback
        def _expire(_now: datetime) -> None:
            if self._awaited.get(key) is awaited:
                awaited.cancel_timer = None
                self._drop(key, DROP_EXPIRED)

        awaited.cancel_timer = async_call_later(self._hass, timeout_s, _expire)
        self._awaited[key] = awaited
        _LOGGER.debug("Awaiting a %s reply from %s for %s s", kind, key, timeout_s)
        return True

    @callback
    def async_take(self, origin: RequestOrigin | None) -> Awaited | None:
        """Remove and return the question awaited from ``origin``, if any."""
        key = origin_key(origin)
        if key is None:
            return None
        awaited = self._awaited.pop(key, None)
        if awaited is not None and awaited.cancel_timer is not None:
            awaited.cancel_timer()
            awaited.cancel_timer = None
        return awaited

    async def async_answer(self, origin: RequestOrigin | None, text: str) -> str | None:
        """Give ``text`` to the question awaited from ``origin``.

        Returns the sentence to speak, or None when nothing was awaited or the
        handler passed the turn on to the language model.
        """
        awaited = self.async_take(origin)
        if awaited is None:
            return None
        return await awaited.handler(text)

    @callback
    def is_awaiting(self, origin: RequestOrigin | None, since: float | None = None) -> bool:
        """Return whether a reply is awaited from ``origin`` (asked at or after ``since``)."""
        key = origin_key(origin)
        awaited = self._awaited.get(key) if key else None
        if awaited is None:
            return False
        return since is None or awaited.created >= since

    @callback
    def async_stop_all(self) -> None:
        """Drop every question (the integration is unloading)."""
        for key in list(self._awaited):
            self._drop(key, DROP_STOPPED)

    @callback
    def _drop(self, key: str, reason: str) -> None:
        awaited = self._awaited.pop(key, None)
        if awaited is None:
            return
        if awaited.cancel_timer is not None:
            awaited.cancel_timer()
            awaited.cancel_timer = None
        _LOGGER.debug("%s reply from %s no longer awaited: %s", awaited.kind, key, reason)
        if awaited.on_drop is not None:
            try:
                awaited.on_drop(reason)
            except Exception:  # noqa: BLE001 - a feature's bookkeeping must not break the turn
                _LOGGER.exception("Error while dropping an awaited %s reply", awaited.kind)


def async_get_awaiting_replies(hass: HomeAssistant) -> AwaitingReplies:
    """Return the shared awaited-replies registry, creating it on first use."""
    registry: AwaitingReplies | None = hass.data.get(DATA_AWAITING_REPLIES)
    if registry is None:
        registry = AwaitingReplies(hass)
        hass.data[DATA_AWAITING_REPLIES] = registry
    return registry
