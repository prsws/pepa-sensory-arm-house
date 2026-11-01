"""Ask before acting on what cannot be undone.

An entity is reversible only when it carries the user-declared ``reverse``
label. Without it the entity is not reversible: that is Pepa's default, so a new
or forgotten device fails closed.

For an entity that is not reversible, ``ha_control`` does not call the service.
It hands the action to the confirmation gate, which:

1. holds the exact action (entity, service and data) and tells the model to ask
   the user a yes-or-no question;
2. waits for the user's next words from the same satellite or conversation (see
   awaiting_reply), for a configurable time (1 to 60 s, default 10 s);
3. decides in code, not in the model, whether those words are a yes:
   - yes: runs exactly the held action and answers with a fixed sentence;
   - no: cancels it and says so;
   - anything else: cancels it silently and the turn goes to the model as usual.

The model cannot approve anything: it only phrases the question.

Requests that arrive through the integration's service (automations, scripts,
other arms) cannot be confirmed, because nobody is there to answer. They are
refused, whoever sent them: the Sensory Arm's authority limits bind every caller.

The decision is made by a ``ReplyDecider``. The one in use matches the whole
reply against fixed word lists in ``confirm_words.yaml``. A decision model can
take its place later (see ``ModelReplyDecider``).

Every step fires a ``pepa_confirm`` event. Nothing failed when an action is held
or cancelled, so no notification is created.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final, Protocol

import yaml
from homeassistant.core import HomeAssistant, callback

from .awaiting_reply import async_get_awaiting_replies
from .request_origin import RequestOrigin
from .verify_alert import _language as reply_language

_LOGGER = logging.getLogger(__name__)

DATA_CONFIRMATION_GATE: Final = "pepa_confirmation_gate"

KIND_CONFIRM: Final = "confirm"
EVENT_PEPA_CONFIRM: Final = "pepa_confirm"

# Seconds to wait for the answer: configurable within these limits.
CONFIRM_TIMEOUT_MIN: Final = 1
CONFIRM_TIMEOUT_MAX: Final = 60
CONFIRM_TIMEOUT_DEFAULT: Final = 10

DECISION_YES: Final = "yes"
DECISION_NO: Final = "no"
DECISION_OTHER: Final = "other"

# pepa_confirm outcomes.
OUTCOME_HELD: Final = "held"
OUTCOME_REFUSED: Final = "refused"
OUTCOME_CONFIRMED: Final = "confirmed"
OUTCOME_DECLINED: Final = "declined"
OUTCOME_CANCELLED: Final = "cancelled"
OUTCOME_FAILED: Final = "failed"
# Plus the awaiting_reply drop reasons: expired, replaced, stopped.

CONFIRM_WORDS_FILE: Final = Path(__file__).parent / "confirm_words.yaml"

_SENTENCES: Final[dict[str, dict[str, str]]] = {
    "es": {
        "done": "Listo.",
        "started": "Dale, ya lo mandé. Te aviso si no termina.",
        "sent": "Enviado.",
        "declined": "Ok, no lo hago.",
        "failed": "No pude hacerlo.",
    },
    "en": {
        "done": "Done.",
        "started": "On it. I'll tell you if it doesn't finish.",
        "sent": "Sent.",
        "declined": "Okay, I won't.",
        "failed": "I couldn't do it.",
    },
}


def clamp_timeout(value: Any) -> int:
    """Return a usable wait in seconds (1 to 60; the default when unreadable)."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return CONFIRM_TIMEOUT_DEFAULT
    return max(CONFIRM_TIMEOUT_MIN, min(CONFIRM_TIMEOUT_MAX, seconds))


def normalize(text: str) -> str:
    """Lower-case, drop accents and punctuation, and collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    plain = plain.replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w]+", " ", plain).split())


class ReplyDecider(Protocol):
    """Decides whether a reply is a yes, a no, or something else."""

    async def async_decide(self, text: str, language: str | None) -> str:
        """Return DECISION_YES, DECISION_NO or DECISION_OTHER."""


class WordListDecider:
    """Matches the whole reply against the fixed lists in confirm_words.yaml."""

    def __init__(self, hass: HomeAssistant, path: Path = CONFIRM_WORDS_FILE) -> None:
        """Initialize; the lists are read on first use, off the event loop."""
        self._hass = hass
        self._path = path
        self._yes: frozenset[str] | None = None
        self._no: frozenset[str] = frozenset()

    def _load(self) -> tuple[frozenset[str], frozenset[str]]:
        with open(self._path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        def _phrases(section: Any) -> frozenset[str]:
            phrases: set[str] = set()
            if isinstance(section, dict):
                for words in section.values():
                    for word in words or ():
                        normalized = normalize(str(word))
                        if normalized:
                            phrases.add(normalized)
            return frozenset(phrases)

        return _phrases(data.get("yes")), _phrases(data.get("no"))

    async def async_decide(self, text: str, language: str | None) -> str:
        """Return the decision for ``text`` (both languages are always accepted)."""
        if self._yes is None:
            self._yes, self._no = await self._hass.async_add_executor_job(self._load)
        reply = normalize(text)
        if reply in self._yes:
            return DECISION_YES
        if reply in self._no:
            return DECISION_NO
        return DECISION_OTHER


class ModelReplyDecider:
    """Placeholder for a decision model that classifies the reply.

    Not wired in. A model could read replies the word lists miss ("sí, pero
    rápido"), but it must keep the same contract: anything it is not sure is a
    yes is DECISION_OTHER, so a doubtful reply still cancels.
    """

    async def async_decide(self, text: str, language: str | None) -> str:
        """Not implemented yet."""
        raise NotImplementedError("A decision model for replies is not built yet")


class ConfirmationGate:
    """Holds actions that cannot be undone until the user confirms them."""

    def __init__(self, hass: HomeAssistant, decider: ReplyDecider | None = None) -> None:
        """Initialize with the word-list decider unless another is given."""
        self._hass = hass
        self.decider: ReplyDecider = decider or WordListDecider(hass)

    @callback
    def async_hold(
        self,
        *,
        origin: RequestOrigin | None,
        entity_id: str,
        name: str,
        action: str,
        run: Callable[[], Awaitable[dict[str, Any]]],
        timeout_s: Any,
    ) -> dict[str, Any]:
        """Hold the action and return what the model is told.

        ``run`` performs exactly the held action when the user confirms it.
        """
        base = {"entity_id": entity_id, "action": action}
        timeout = clamp_timeout(timeout_s)

        @callback
        def _on_drop(reason: str) -> None:
            self._fire(reason, origin, base)

        async def _on_reply(text: str) -> str | None:
            return await self._async_on_reply(text, origin, base, run)

        awaiting = async_get_awaiting_replies(self._hass)
        if not awaiting.async_expect(origin, KIND_CONFIRM, _on_reply, timeout, _on_drop):
            self._fire(OUTCOME_REFUSED, origin, base)
            _LOGGER.info("Refused %s on %s: cannot be confirmed from here", action, entity_id)
            return {
                "success": False,
                "confirmation_required": True,
                **base,
                "message": (
                    f"Not done. {name} is not marked as reversible, so it needs a person's "
                    f"confirmation, and this request came through a channel where nobody can "
                    f"confirm it. Report that it was not done and needs a person to confirm."
                ),
            }

        self._fire(OUTCOME_HELD, origin, base, timeout_s=timeout)
        _LOGGER.info("Held %s on %s for confirmation (%s s)", action, entity_id, timeout)
        return {
            "success": False,
            "confirmation_required": True,
            **base,
            "message": (
                f"Not done yet. {name} is not marked as reversible, so the user must confirm "
                f"first. Ask the user one short yes-or-no question to confirm ({action} "
                f"{name}). Do not call ha_control again for this request: the user's answer "
                f"is handled automatically."
            ),
        }

    async def _async_on_reply(
        self,
        text: str,
        origin: RequestOrigin | None,
        base: dict[str, Any],
        run: Callable[[], Awaitable[dict[str, Any]]],
    ) -> str | None:
        language = reply_language(self._hass, origin)
        sentences = _SENTENCES[language]
        try:
            decision = await self.decider.async_decide(text, origin.language if origin else None)
        except Exception:  # noqa: BLE001 - a broken decider must not approve anything
            _LOGGER.exception("Could not decide the confirmation reply; cancelling")
            decision = DECISION_OTHER

        if decision == DECISION_NO:
            self._fire(OUTCOME_DECLINED, origin, base)
            return sentences["declined"]
        if decision != DECISION_YES:
            self._fire(OUTCOME_CANCELLED, origin, base)
            return None

        try:
            result = await run()
        except Exception as err:  # noqa: BLE001 - say it out loud instead of failing the turn
            _LOGGER.error("Confirmed %s on %s failed: %s", base["action"], base["entity_id"], err)
            self._fire(OUTCOME_FAILED, origin, base, error=str(err))
            return sentences["failed"]

        self._fire(OUTCOME_CONFIRMED, origin, base)
        verification = result.get("verification")
        if verification == "pending":
            return sentences["started"]
        if verification == "unverifiable":
            return sentences["sent"]
        return sentences["done"]

    @callback
    def _fire(
        self, outcome: str, origin: RequestOrigin | None, base: dict[str, Any], **extra: Any
    ) -> None:
        self._hass.bus.async_fire(
            EVENT_PEPA_CONFIRM,
            {
                "outcome": outcome,
                **base,
                "channel": origin.channel if origin else None,
                "satellite_id": origin.satellite_id if origin else None,
                "conversation_id": origin.conversation_id if origin else None,
                **extra,
            },
        )


def async_get_confirmation_gate(hass: HomeAssistant) -> ConfirmationGate:
    """Return the shared confirmation gate, creating it on first use."""
    gate: ConfirmationGate | None = hass.data.get(DATA_CONFIRMATION_GATE)
    if gate is None:
        gate = ConfirmationGate(hass)
        hass.data[DATA_CONFIRMATION_GATE] = gate
    return gate
