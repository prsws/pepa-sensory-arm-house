"""Local handling guard: keep Home Assistant's fast path from skipping the gate.

With "Prefer handling commands locally" on, Home Assistant tries its own
built-in sentences before the pipeline's agent sees a request, and acts by
itself when they match. For a pipeline whose agent is this integration, that
would skip the confirmation gate. The guard sits in Home Assistant's intent
handlers and, only for requests meant for one of this integration's agents:

- Device actions (turn on/off/toggle, light, fan, cover, media, vacuum...):
  run locally only when every target carries the ``reverse`` label.
- Other intents: only questions run locally (``READ_INTENTS``).
- Anything else is refused before anything is called, by raising
  ``intent.MatchFailedError``. Home Assistant then reports "no valid targets",
  drops its own answer and passes the request on to the agent, which holds the
  action and asks. The person hears nothing about the refusal.

Requests meant for any other agent are not touched. The design and the Home
Assistant hook points it relies on are in docs/LOCAL_HANDLING_GUARD.md; the hook
points are pinned by tests/integration/test_local_guard_hooks.py.

Lifecycle (called from the integration's setup and unload):

- ``async_enable`` runs when an entry of this integration loads. It adds the
  entry's agent id to the guard and wraps every intent handler registered so
  far. The first entry also starts a listener that wraps handlers added later,
  whenever another integration finishes loading.
- ``async_disable`` runs when an entry unloads. It removes the entry's agent
  id. When the last entry is gone, it stops the listener and puts every
  wrapped handler back exactly as it was.

What it logs: one INFO line when the guard is switched on or off, with how many
handlers it wraps, and a DEBUG line each time it hands a request to the agent,
naming the intent and why.

Startup check (``async_report``): once Home Assistant has started, and again
whenever another integration loads, the guard checks that the Home Assistant
pieces it relies on are still there and that every handler is wrapped (or is a
question). If anything is wrong it raises a Home Assistant repair notice saying
that local handling is not guarded for Pepa's voice assistants, and logs an
ERROR; when all is well again the notice is removed. It never fails silently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .house_catalog import LABEL_REVERSE

_LOGGER = logging.getLogger(__name__)

DATA_LOCAL_GUARD: Final = "pepa_local_guard"

# Repair notice raised when the guard cannot do its job (see async_report).
ISSUE_NOT_GUARDING: Final = "local_guard_not_guarding"

# Marks a handler method this module has wrapped, so it is never wrapped twice.
_GUARDED_MARK: Final = "_pepa_local_guard"

Handler = Callable[..., Awaitable[intent.IntentResponse]]

# Intents that only answer a question and change nothing. Through a Pepa
# pipeline these are the only intents, other than device actions on reversible
# targets, that Home Assistant may answer by itself. Anything not listed,
# including intents a later Home Assistant release adds, goes to the agent.
READ_INTENTS: Final = frozenset(
    {
        intent.INTENT_GET_STATE,
        intent.INTENT_GET_TEMPERATURE,
        intent.INTENT_GET_CURRENT_TIME,
        intent.INTENT_GET_CURRENT_DATE,
        intent.INTENT_NEVERMIND,
        "HassGetWeather",
    }
)


@dataclass
class LocalGuard:
    """Which agents the guard protects, and which handlers it has wrapped."""

    agent_ids: set[str] = field(default_factory=set)
    # handler -> (wrapped method name, original method), kept to restore on removal.
    wrapped: dict[intent.IntentHandler, tuple[str, Handler]] = field(default_factory=dict)
    # Stops the listener that wraps handlers registered after the guard started.
    unsub_component_loaded: CALLBACK_TYPE | None = None
    # Stops the one-time check that runs when Home Assistant has started.
    unsub_started: CALLBACK_TYPE | None = None

    @property
    def active(self) -> bool:
        """True while at least one entry of this integration is loaded."""
        return bool(self.agent_ids)

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
            _LOGGER.debug(
                "Local handling guard: %s on %s handed to the agent (not every target"
                " carries the reverse label)",
                intent_obj.intent_type,
                [state.entity_id for state in match_result.states],
            )
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
    guard.wrapped[handler] = ("async_handle_states", original)
    return True


@callback
def async_guard_other_handler(guard: LocalGuard, handler: intent.IntentHandler) -> bool:
    """Wrap an intent handler that is not a service intent. Returns True if wrapped.

    These handlers resolve their targets and act inside their own
    ``async_handle``, with no common step before acting, so the guard cannot
    check targets. Through a Pepa pipeline only the read intents pass; any
    other intent (setting a thermostat or a humidifier, timers, lists...) is
    refused before it starts and goes to the agent.
    """
    if handler.intent_type in READ_INTENTS:
        return False
    original = handler.async_handle
    if getattr(original, _GUARDED_MARK, False):
        return False

    async def guarded_handle(intent_obj: intent.Intent) -> intent.IntentResponse:
        if guard.is_pepa_request(intent_obj):
            _LOGGER.debug(
                "Local handling guard: %s handed to the agent (only questions are"
                " answered locally)",
                intent_obj.intent_type,
            )
            raise intent.MatchFailedError(
                result=intent.MatchTargetsResult(
                    is_match=False, no_match_reason=intent.MatchFailedReason.ASSISTANT
                ),
                constraints=intent.MatchTargetsConstraints(),
            )
        return await original(intent_obj)

    setattr(guarded_handle, _GUARDED_MARK, True)
    handler.async_handle = guarded_handle  # type: ignore[method-assign]
    guard.wrapped[handler] = ("async_handle", original)
    return True


@callback
def async_guard_registered_handlers(hass: HomeAssistant, guard: LocalGuard) -> int:
    """Wrap every intent handler registered so far. Returns how many were wrapped."""
    count = 0
    for handler in list(intent.async_get(hass)):
        if isinstance(handler, intent.DynamicServiceIntentHandler):
            count += async_guard_service_handler(hass, guard, handler)
        else:
            count += async_guard_other_handler(guard, handler)
    return count


@callback
def async_enable(hass: HomeAssistant, agent_id: str) -> None:
    """Switch the guard on for one of this integration's agents.

    Called on entry setup with the entry id, which is the agent id Home
    Assistant records on every request through that agent's pipelines.
    """
    guard = async_get_local_guard(hass)
    first = not guard.active
    guard.agent_ids.add(agent_id)
    count = async_guard_registered_handlers(hass, guard)

    if first:

        @callback
        def _wrap_new_handlers(event: Event) -> None:
            """Another integration finished loading: wrap any handler it added."""
            if added := async_guard_registered_handlers(hass, guard):
                _LOGGER.debug(
                    "Local handling guard: wrapped %d handler(s) added by %s",
                    added,
                    event.data.get("component"),
                )
            if hass.is_running:
                async_report(hass, guard)

        @callback
        def _check_on_start(_event: Event) -> None:
            """Home Assistant has started: wrap any stragglers, then check."""
            guard.unsub_started = None
            async_guard_registered_handlers(hass, guard)
            async_report(hass, guard)

        guard.unsub_component_loaded = hass.bus.async_listen(
            EVENT_COMPONENT_LOADED, _wrap_new_handlers
        )
        if hass.is_running:
            async_report(hass, guard)
        else:
            guard.unsub_started = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _check_on_start
            )
        _LOGGER.info(
            "Local handling guard on: %d intent handler(s) wrapped; Home Assistant"
            " answers Pepa's pipelines by itself only for questions and for actions"
            " on entities labelled reverse",
            count,
        )


@callback
def async_disable(hass: HomeAssistant, agent_id: str) -> None:
    """Switch the guard off for one agent; restore everything after the last."""
    guard = async_get_local_guard(hass)
    guard.agent_ids.discard(agent_id)
    if guard.active:
        return

    if guard.unsub_component_loaded is not None:
        guard.unsub_component_loaded()
        guard.unsub_component_loaded = None
    if guard.unsub_started is not None:
        guard.unsub_started()
        guard.unsub_started = None
    ir.async_delete_issue(hass, DOMAIN, ISSUE_NOT_GUARDING)

    restored = 0
    for handler, (name, original) in guard.wrapped.items():
        # The wrapper was set on the instance; removing it brings back the
        # class's own method. Set the original back only if that is not it.
        if name in vars(handler):
            delattr(handler, name)
        if getattr(handler, name) != original:
            setattr(handler, name, original)
        restored += 1
    guard.wrapped.clear()
    _LOGGER.info("Local handling guard off: %d intent handler(s) restored", restored)


@callback
def async_check(hass: HomeAssistant, guard: LocalGuard) -> list[str]:
    """Return what keeps the guard from working; an empty list means it works.

    Checks the Home Assistant pieces the guard relies on (also pinned by
    tests/integration/test_local_guard_hooks.py) and that every registered
    handler is either wrapped or a question.
    """
    problems: list[str] = []
    if not callable(getattr(intent.DynamicServiceIntentHandler, "async_handle_states", None)):
        problems.append("service intents no longer show their targets before acting")
    if "conversation_agent_id" not in getattr(intent.Intent, "__slots__", ()):
        problems.append("requests no longer say which agent they are for")
    try:
        from homeassistant.components.conversation import default_agent

        codes = getattr(default_agent, "_MATCH_ERROR_CODES", None)
    except ImportError:
        codes = None
    if not codes or intent.IntentResponseErrorCode.NO_VALID_TARGETS not in codes:
        problems.append("a refused request may no longer be handed to the agent")

    unguarded = sorted(
        handler.intent_type
        for handler in intent.async_get(hass)
        if handler not in guard.wrapped and handler.intent_type not in READ_INTENTS
    )
    if unguarded:
        problems.append("not guarded: " + ", ".join(unguarded))
    return problems


@callback
def async_report(hass: HomeAssistant, guard: LocalGuard) -> bool:
    """Raise or clear the repair notice. Returns True when the guard works."""
    problems = async_check(hass, guard)
    if not problems:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_NOT_GUARDING)
        return True
    summary = "; ".join(problems)
    _LOGGER.error(
        "Local handling guard is not guarding Pepa's voice assistants: %s. Until this"
        " is fixed, turn off 'Prefer handling commands locally' in those assistants",
        summary,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_NOT_GUARDING,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_NOT_GUARDING,
        translation_placeholders={"problems": summary},
    )
    return False
