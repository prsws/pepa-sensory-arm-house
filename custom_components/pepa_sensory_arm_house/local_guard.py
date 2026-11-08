"""Local handling guard: keep Home Assistant's fast path from skipping the gate.

With "Prefer handling commands locally" on, Home Assistant tries its own
built-in sentences before the pipeline's agent sees a request, and acts by
itself when they match. For a pipeline whose agent is this integration, that
would skip the confirmation gate. The guard sits in Home Assistant's intent
handlers and, only for requests meant for one of this integration's agents:

- lets an action run locally when every target carries the ``reverse`` label;
- otherwise refuses before anything is called, by raising
  ``intent.MatchFailedError``. Home Assistant then reports "no valid targets",
  drops its own answer and passes the request on to the agent, which holds the
  action and asks.

Requests meant for any other agent are not touched. The design and the Home
Assistant hook points it relies on are in docs/LOCAL_HANDLING_GUARD.md; the hook
points are pinned by tests/integration/test_local_guard_hooks.py.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent

from .house_catalog import LABEL_REVERSE

DATA_LOCAL_GUARD: Final = "pepa_local_guard"

# Marks a handler method this module has wrapped, so it is never wrapped twice.
_GUARDED_MARK: Final = "_pepa_local_guard"

HandleStates = Callable[..., Awaitable[intent.IntentResponse]]


@dataclass
class LocalGuard:
    """Which agents the guard protects, and which handlers it has wrapped."""

    agent_ids: set[str] = field(default_factory=set)
    # handler -> its original async_handle_states, kept to restore on removal.
    wrapped: dict[intent.DynamicServiceIntentHandler, HandleStates] = field(default_factory=dict)

    def is_pepa_request(self, intent_obj: intent.Intent) -> bool:
        """True when the request came through a pipeline for one of our agents."""
        return intent_obj.conversation_agent_id in self.agent_ids


@callback
def async_get_local_guard(hass: HomeAssistant) -> LocalGuard:
    """Return the shared guard, creating it on first use."""
    if (guard := hass.data.get(DATA_LOCAL_GUARD)) is None:
        guard = hass.data[DATA_LOCAL_GUARD] = LocalGuard()
    return guard


@callback
def async_all_reversible(hass: HomeAssistant, states: Iterable[State]) -> bool:
    """True only if every entity carries the ``reverse`` label.

    No registry entry, or no label, means not reversible: the guard fails
    closed exactly as the confirmation gate does. An empty target list is not
    treated as reversible either.
    """
    registry = er.async_get(hass)
    entity_ids = [state.entity_id for state in states]
    if not entity_ids:
        return False
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is None or LABEL_REVERSE not in entry.labels:
            return False
    return True


@callback
def async_guard_service_handler(
    hass: HomeAssistant, guard: LocalGuard, handler: intent.DynamicServiceIntentHandler
) -> bool:
    """Wrap a service intent handler's per-target step. Returns True if wrapped.

    ``async_handle_states`` receives every matched entity before any service is
    called, so the decision is made for the whole request at once: a mixed
    request (something reversible and something not) goes to the agent whole.
    """
    original = handler.async_handle_states
    if getattr(original, _GUARDED_MARK, False):
        return False

    async def guarded_handle_states(
        intent_obj: intent.Intent,
        match_result: intent.MatchTargetsResult,
        match_constraints: intent.MatchTargetsConstraints,
        match_preferences: intent.MatchTargetsPreferences | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> intent.IntentResponse:
        if guard.is_pepa_request(intent_obj) and not async_all_reversible(
            hass, match_result.states
        ):
            raise intent.MatchFailedError(
                result=match_result,
                constraints=match_constraints,
                preferences=match_preferences,
            )
        return await original(
            intent_obj, match_result, match_constraints, match_preferences, *args, **kwargs
        )

    setattr(guarded_handle_states, _GUARDED_MARK, True)
    handler.async_handle_states = guarded_handle_states  # type: ignore[method-assign]
    guard.wrapped[handler] = original
    return True


@callback
def async_guard_registered_handlers(hass: HomeAssistant, guard: LocalGuard) -> int:
    """Wrap every service intent handler registered so far. Returns how many."""
    count = 0
    for handler in list(intent.async_get(hass)):
        if isinstance(handler, intent.DynamicServiceIntentHandler):
            count += async_guard_service_handler(hass, guard, handler)
    return count
