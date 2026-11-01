"""Where the request being processed came from.

The agent records the origin of each request when it starts processing it, and
anything running for that request (the tools in particular) can read it with
``current_origin()``. Later steps use it to answer through the same channel the
request came in on, for example a loud ``verify`` failure spoken on the
satellite that heard the command.

The origin is held in a context variable, so it is task-local: two requests
processed at the same time (two satellites, or a satellite and an automation)
each see their own origin, and the tasks a request starts (tool calls run under
``asyncio.wait_for`` and ``asyncio.gather``) inherit it.

Channels:
- ``satellite``: spoken to a voice satellite; ``satellite_id`` is its
  ``assist_satellite`` entity.
- ``conversation``: through Home Assistant's conversation pipeline without a
  satellite (for example typed in the app).
- ``service``: the integration's own service, typically an automation or script.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from homeassistant.components.conversation import ConversationInput

CHANNEL_SATELLITE: Final = "satellite"
CHANNEL_CONVERSATION: Final = "conversation"
CHANNEL_SERVICE: Final = "service"


@dataclass(frozen=True)
class RequestOrigin:
    """Where one request came from."""

    channel: str
    satellite_id: str | None = None
    device_id: str | None = None
    conversation_id: str | None = None
    # The pipeline's configured language (e.g. "es"); None for the service
    # channel, where there is no pipeline.
    language: str | None = None

    @classmethod
    def from_conversation_input(cls, user_input: ConversationInput) -> RequestOrigin:
        """Build the origin of a request that came through the conversation pipeline."""
        satellite_id = getattr(user_input, "satellite_id", None)
        return cls(
            channel=CHANNEL_SATELLITE if satellite_id else CHANNEL_CONVERSATION,
            satellite_id=satellite_id,
            device_id=getattr(user_input, "device_id", None),
            conversation_id=getattr(user_input, "conversation_id", None),
            language=getattr(user_input, "language", None),
        )


_current_origin: ContextVar[RequestOrigin | None] = ContextVar("pepa_request_origin", default=None)


def current_origin() -> RequestOrigin | None:
    """Return the origin of the request being processed, or None outside a request."""
    return _current_origin.get()


@contextmanager
def request_origin(origin: RequestOrigin) -> Iterator[RequestOrigin]:
    """Record ``origin`` for everything processed inside the block."""
    token = _current_origin.set(origin)
    try:
        yield origin
    finally:
        _current_origin.reset(token)
