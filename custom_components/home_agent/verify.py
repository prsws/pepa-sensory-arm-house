"""Post-dispatch verification for slow-acting devices (the ``verify`` label).

Some devices take time to do what they were told: a carport gate opens over
many seconds; a lock can jam. For an entity carrying the user-declared
``verify`` label, ``ha_control`` sends the command, then starts a check here
and returns at once — the conversation never waits for the device.

The check compares the entity's state with the state the command implies
(``open_cover`` -> ``open``, ``lock`` -> ``locked``, ``turn_on`` -> ``on``...):

- confirmed: the expected state arrives (immediately, or within the deadline)
- failed: the entity becomes ``unavailable`` or ``jammed``, or the deadline
  (60 seconds) passes without the expected state
- superseded: a newer command reaches the same entity first
- unverifiable: the command has no implied state in the table below

Nothing here blocks: the check is a state-change subscription plus one timer,
both Home Assistant callbacks. Every outcome fires a ``pepa_verify`` event (the
temporary ledger until the arm's ledger exists) carrying the entity, the
command, the expected and observed states, the time taken, the request's
origin (including its pipeline language) and the alert channels used.

Failures are loud (see verify_alert): logged as errors, always a Home Assistant
notification, and spoken on the satellite the request came from when it can
announce.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import Any, Final

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .request_origin import RequestOrigin
from .verify_alert import async_alert_failure

_LOGGER = logging.getLogger(__name__)

DATA_VERIFY_MANAGER: Final = "pepa_verify_manager"

# Stable Pepa event name, independent of the integration's domain name.
EVENT_VERIFY: Final = "pepa_verify"

VERIFY_TIMEOUT_SECONDS: Final = 60

OUTCOME_PENDING: Final = "pending"
OUTCOME_CONFIRMED: Final = "confirmed"
OUTCOME_FAILED: Final = "failed"
OUTCOME_SUPERSEDED: Final = "superseded"
OUTCOME_UNVERIFIABLE: Final = "unverifiable"

REASON_TIMEOUT: Final = "timeout"

# States that end a check as failed without waiting for the deadline.
FAIL_STATES: Final = frozenset({"unavailable", "jammed"})


@dataclass(frozen=True)
class Expectation:
    """The result a command implies: a state, or an attribute value."""

    description: str
    state: str | None = None
    attribute: str | None = None
    value: Any = None

    def is_met(self, current: State) -> bool:
        """Return True if the entity's current state satisfies the expectation."""
        if self.attribute is not None:
            return current.attributes.get(self.attribute) == self.value
        return current.state == self.state

    def observed(self, current: State | None) -> Any:
        """Return what was observed, in the expectation's own terms."""
        if current is None:
            return None
        if self.attribute is not None:
            return current.attributes.get(self.attribute)
        return current.state


def _state(name: str) -> Expectation:
    return Expectation(description=name, state=name)


def expected_outcome(
    domain: str, service: str, service_data: dict[str, Any], previous_state: str | None
) -> Expectation | None:
    """Return the outcome a command implies, or None if it implies none we check.

    The table grows case by case as real devices need it.
    """
    if domain == "cover":
        if service == "open_cover":
            return _state("open")
        if service == "close_cover":
            return _state("closed")
        if service == "set_cover_position" and "position" in service_data:
            position = service_data["position"]
            return Expectation(
                description=f"position {position}", attribute="current_position", value=position
            )
        if service == "toggle":
            if previous_state in ("open", "opening"):
                return _state("closed")
            if previous_state in ("closed", "closing"):
                return _state("open")
        return None

    if domain == "lock":
        if service == "lock":
            return _state("locked")
        if service == "unlock":
            return _state("unlocked")
        return None

    if service == "turn_on":
        return _state("on")
    if service == "turn_off":
        return _state("off")
    if service == "toggle":
        if previous_state == "on":
            return _state("off")
        if previous_state == "off":
            return _state("on")
    return None


@dataclass
class _Check:
    entity_id: str
    command: str
    expectation: Expectation
    origin: RequestOrigin | None
    started: float
    unsubscribers: list[CALLBACK_TYPE] = field(default_factory=list)


class VerifyManager:
    """Runs at most one check per entity; a newer command supersedes the older check."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self.hass = hass
        self._checks: dict[str, _Check] = {}

    @property
    def pending_entity_ids(self) -> frozenset[str]:
        """Return the entities with a check in progress."""
        return frozenset(self._checks)

    @callback
    def async_start(
        self,
        entity_id: str,
        service: str,
        service_data: dict[str, Any],
        previous_state: str | None,
        origin: RequestOrigin | None,
    ) -> str:
        """Start checking a command just dispatched; return the outcome so far.

        Returns OUTCOME_PENDING, or OUTCOME_CONFIRMED if the entity is already in
        the expected state, or OUTCOME_UNVERIFIABLE.
        """
        domain = entity_id.split(".", 1)[0]
        command = f"{domain}.{service}"
        self._async_supersede(entity_id)

        expectation = expected_outcome(domain, service, service_data, previous_state)
        if expectation is None:
            _LOGGER.warning(
                "verify: %s has the verify label but %s implies no state to check",
                entity_id,
                command,
            )
            self._fire(entity_id, command, None, origin, OUTCOME_UNVERIFIABLE, None, None, 0.0)
            return OUTCOME_UNVERIFIABLE

        check = _Check(entity_id, command, expectation, origin, time.monotonic())
        current = self.hass.states.get(entity_id)
        if current is not None and expectation.is_met(current):
            self._finish(check, OUTCOME_CONFIRMED, None, current)
            return OUTCOME_CONFIRMED

        self._checks[entity_id] = check
        check.unsubscribers.append(
            async_track_state_change_event(
                self.hass, [entity_id], partial(self._async_on_state_change, check)
            )
        )
        check.unsubscribers.append(
            async_call_later(
                self.hass, VERIFY_TIMEOUT_SECONDS, partial(self._async_on_timeout, check)
            )
        )
        return OUTCOME_PENDING

    @callback
    def async_stop_all(self) -> None:
        """Cancel every check without an outcome (the integration is unloading)."""
        for check in list(self._checks.values()):
            self._release(check)

    @callback
    def _async_on_state_change(self, check: _Check, event: Event) -> None:
        if self._checks.get(check.entity_id) is not check:
            return
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        if check.expectation.is_met(new_state):
            self._finish(check, OUTCOME_CONFIRMED, None, new_state)
        elif new_state.state in FAIL_STATES:
            self._finish(check, OUTCOME_FAILED, new_state.state, new_state)

    @callback
    def _async_on_timeout(self, check: _Check, _now: Any) -> None:
        if self._checks.get(check.entity_id) is not check:
            return
        current = self.hass.states.get(check.entity_id)
        if current is not None and check.expectation.is_met(current):
            self._finish(check, OUTCOME_CONFIRMED, None, current)
        else:
            self._finish(check, OUTCOME_FAILED, REASON_TIMEOUT, current)

    @callback
    def _async_supersede(self, entity_id: str) -> None:
        check = self._checks.get(entity_id)
        if check is not None:
            self._finish(check, OUTCOME_SUPERSEDED, None, self.hass.states.get(entity_id))

    @callback
    def _release(self, check: _Check) -> None:
        for unsubscribe in check.unsubscribers:
            unsubscribe()
        check.unsubscribers.clear()
        if self._checks.get(check.entity_id) is check:
            del self._checks[check.entity_id]

    @callback
    def _finish(
        self, check: _Check, outcome: str, reason: str | None, current: State | None
    ) -> None:
        self._release(check)
        duration = time.monotonic() - check.started
        observed = check.expectation.observed(current)
        alerts = None
        if outcome == OUTCOME_FAILED:
            alerts = async_alert_failure(
                self.hass,
                check.origin,
                check.entity_id,
                check.expectation.description,
                observed,
                reason,
                VERIFY_TIMEOUT_SECONDS,
            )
        self._fire(
            check.entity_id,
            check.command,
            check.expectation.description,
            check.origin,
            outcome,
            reason,
            observed,
            duration,
            alerts,
        )
        if outcome == OUTCOME_FAILED:
            _LOGGER.error(
                "verify FAILED: %s after %s — expected %s, observed %s (%s, %.1f s)",
                check.entity_id,
                check.command,
                check.expectation.description,
                observed,
                reason,
                duration,
            )
        else:
            _LOGGER.info(
                "verify %s: %s after %s (%.1f s)", outcome, check.entity_id, check.command, duration
            )

    @callback
    def _fire(
        self,
        entity_id: str,
        command: str,
        expected: str | None,
        origin: RequestOrigin | None,
        outcome: str,
        reason: str | None,
        observed: Any,
        duration: float,
        alerts: list[str] | None = None,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_VERIFY,
            {
                "entity_id": entity_id,
                "command": command,
                "outcome": outcome,
                "reason": reason,
                "expected": expected,
                "observed": observed,
                "duration_s": round(duration, 1),
                "origin": asdict(origin) if origin is not None else None,
                "alerts": alerts or [],
            },
        )


@callback
def async_get_verify_manager(hass: HomeAssistant) -> VerifyManager:
    """Return the shared verify manager, creating it on first use."""
    manager: VerifyManager | None = hass.data.get(DATA_VERIFY_MANAGER)
    if manager is None:
        manager = VerifyManager(hass)
        hass.data[DATA_VERIFY_MANAGER] = manager
    return manager
