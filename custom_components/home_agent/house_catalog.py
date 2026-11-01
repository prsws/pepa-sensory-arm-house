"""House catalog: one answer to "what does the house expose to the agent?".

The catalog computes the set of entities exposed to Assist (the conversation
assistant) and, from it, a CSV that describes each one. Both the tools'
allow-list and the prompt's device catalog read this same answer, so what the
model is shown and what the tools will let it touch cannot drift apart.

The CSV is published as ``sensor.pepa_entity_context`` (state = entity count,
attribute ``csv`` = the table), the contract PSA's pyscript ``entities_list``
used to fulfil. It is kept byte-identical between turns and rebuilt only when
something changed, so the prompt prefix stays cacheable.

When it is refreshed:
- every conversation turn, by the agent (a cheap exposed-set check; the CSV is
  rebuilt only if the set changed or a change was flagged);
- when Assist exposure settings change (Home Assistant's own listener, so no
  timer is needed);
- when the entity, device or area registries change (aliases, labels, areas),
  coalesced so a burst of registry events causes one rebuild;
- once Home Assistant has finished starting.

CSV columns: entity_id, name, area, aliases, domain, services, volatile, verify.
The first seven are PSA's; ``verify`` is new: ``true`` when the entity carries the
user-declared ``verify`` label (slow-acting devices whose result must be checked
some time after the action is dispatched).
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Final

from homeassistant.components.homeassistant import exposed_entities
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.start import async_at_started

_LOGGER = logging.getLogger(__name__)

# The assistant whose exposure settings define the catalog (Assist).
ASSISTANT: Final = "conversation"

# Kept outside hass.data[DOMAIN], which holds one key per config entry and is
# used to detect when the last entry is unloaded.
DATA_HOUSE_CATALOG: Final = "pepa_house_catalog"

SENSOR_ENTITY_ID: Final = "sensor.pepa_entity_context"
SENSOR_FRIENDLY_NAME: Final = "Pepa Entity Context"

# User-declared labels (Home Assistant label ids).
LABEL_VOLATILE: Final = "volatile"
LABEL_VERIFY: Final = "verify"

CSV_COLUMNS: Final = (
    "entity_id",
    "name",
    "area",
    "aliases",
    "domain",
    "services",
    "volatile",
    "verify",
)

UNASSIGNED_AREA: Final = "Unassigned"

# Seconds a burst of registry or exposure changes is coalesced before one rebuild.
REBUILD_COOLDOWN: Final = 1.0

# Per-domain services, pipe-delimited (commas would break the CSV column).
# Carried over unchanged from PSA's entities_list pyscript.
_DOMAIN_SERVICES: Final[dict[str, str]] = {
    "light": "turn_on|turn_off|toggle|turn_on[brightness_pct]|turn_on[rgb_color]",
    "fan": "turn_on|turn_off|toggle|set_percentage",
    "climate": "set_temperature|set_hvac_mode|turn_on|turn_off",
    "cover": "open_cover|close_cover|set_cover_position|toggle",
    "media_player": "turn_on|turn_off|play_media|media_pause|media_stop|volume_set",
    "switch": "turn_on|turn_off|toggle",
    "input_boolean": "turn_on|turn_off|toggle",
    "lock": "lock|unlock",
    "binary_sensor": "read_only",
    "sensor": "read_only",
    "weather": "read_only",
}
_DEFAULT_SERVICES: Final = "turn_on|turn_off"


def services_for_domain(domain: str) -> str:
    """Return the pipe-delimited services offered for a domain."""
    return _DOMAIN_SERVICES.get(domain, _DEFAULT_SERVICES)


def _flag(value: bool) -> str:
    return "true" if value else "false"


@callback
def build_catalog_csv(hass: HomeAssistant, exposed: frozenset[str]) -> tuple[str, int]:
    """Build the catalog CSV for the given exposed entity ids.

    Deterministic: rows are sorted by entity_id and aliases are sorted, so the
    same house always produces the same bytes. Returns (csv_text, row_count).
    """
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    rows: list[list[str]] = []
    for entity_id in exposed:
        state = hass.states.get(entity_id)
        if state is None:
            continue

        area_name = UNASSIGNED_AREA
        aliases = ""
        volatile = False
        verify = False

        entry = entity_reg.async_get(entity_id)
        if entry is not None:
            # An area set on the entity overrides the device's area.
            area_id = entry.area_id
            if area_id is None and entry.device_id:
                device = device_reg.async_get(entry.device_id)
                area_id = device.area_id if device is not None else None
            if area_id:
                area = area_reg.async_get_area(area_id)
                if area is not None:
                    area_name = area.name

            # Only plain-text aliases; Home Assistant can also store non-text
            # entries (computed-name markers) that are not words to match.
            aliases = "|".join(sorted(a for a in entry.aliases if isinstance(a, str)))

            volatile = LABEL_VOLATILE in entry.labels
            verify = LABEL_VERIFY in entry.labels

        domain = entity_id.split(".", 1)[0]
        rows.append(
            [
                entity_id,
                str(state.attributes.get("friendly_name", entity_id)),
                area_name,
                aliases,
                domain,
                services_for_domain(domain),
                _flag(volatile),
                _flag(verify),
            ]
        )

    rows.sort(key=lambda row: row[0])

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    writer.writerows(rows)
    return buffer.getvalue().rstrip("\n"), len(rows)


class HouseCatalog:
    """Shared, per-Home-Assistant catalog of exposed entities.

    One instance serves every config entry. It publishes the sensor only while at
    least one config entry is using it; without users (for example in unit tests
    that build an agent directly) it still answers the exposed set but does not
    touch registries or publish.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the catalog."""
        self.hass = hass
        self._exposed: frozenset[str] = frozenset()
        self._csv: str = ""
        self._dirty = True
        self._users: set[str] = set()
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._debouncer: Debouncer | None = None

    @property
    def exposed_entity_ids(self) -> frozenset[str]:
        """Return the exposed entity ids as of the last refresh."""
        return self._exposed

    @property
    def csv(self) -> str:
        """Return the catalog CSV as last published."""
        return self._csv

    @property
    def active(self) -> bool:
        """Return True while at least one config entry is using the catalog."""
        return bool(self._users)

    @callback
    def async_refresh(self) -> frozenset[str]:
        """Recompute the exposed set; rebuild and publish the CSV if needed.

        Cheap when nothing changed: one pass over the states asking Home
        Assistant whether each is exposed. Returns the current exposed set.
        """
        current = frozenset(
            state.entity_id
            for state in self.hass.states.async_all()
            if exposed_entities.async_should_expose(self.hass, ASSISTANT, state.entity_id)
        )
        if current != self._exposed:
            _LOGGER.debug(
                "House catalog: %d exposed entities (+%d, -%d)",
                len(current),
                len(current - self._exposed),
                len(self._exposed - current),
            )
            self._exposed = current
            self._dirty = True

        if self._dirty and self.active:
            self._async_rebuild()

        return self._exposed

    @callback
    def _async_rebuild(self) -> None:
        """Rebuild the CSV and publish it if the bytes changed."""
        self._dirty = False
        csv_text, count = build_catalog_csv(self.hass, self._exposed)
        if csv_text == self._csv and self.hass.states.get(SENSOR_ENTITY_ID) is not None:
            return
        self._csv = csv_text
        self.hass.states.async_set(
            SENSOR_ENTITY_ID,
            count,
            {"csv": csv_text, "count": count, "friendly_name": SENSOR_FRIENDLY_NAME},
        )
        _LOGGER.debug("House catalog published %d exposed entities", count)

    @callback
    def _async_mark_changed(self, *_args: object) -> None:
        """Flag a change and schedule one coalesced refresh."""
        self._dirty = True
        if self._debouncer is not None:
            self._debouncer.async_schedule_call()

    @callback
    def async_add_user(self, entry_id: str) -> None:
        """Register a config entry; the first one starts listening and publishing."""
        first = not self._users
        self._users.add(entry_id)
        if first:
            self._async_start_listening()
        self._dirty = True
        self.async_refresh()

    @callback
    def async_remove_user(self, entry_id: str) -> None:
        """Unregister a config entry; the last one stops listening and removes the sensor."""
        self._users.discard(entry_id)
        if self._users:
            return
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._debouncer is not None:
            self._debouncer.async_cancel()
            self._debouncer = None
        self.hass.states.async_remove(SENSOR_ENTITY_ID)
        self._csv = ""
        self._dirty = True

    @callback
    def _async_start_listening(self) -> None:
        self._debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=REBUILD_COOLDOWN,
            immediate=False,
            function=self.async_refresh,
        )
        self._unsubscribers.append(
            exposed_entities.async_listen_entity_updates(
                self.hass, ASSISTANT, self._async_mark_changed
            )
        )
        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
        ):
            self._unsubscribers.append(
                self.hass.bus.async_listen(event_type, self._async_mark_changed)
            )
        self._unsubscribers.append(async_at_started(self.hass, self._async_mark_changed))


@callback
def async_get_house_catalog(hass: HomeAssistant) -> HouseCatalog:
    """Return the shared catalog, creating it on first use."""
    catalog: HouseCatalog | None = hass.data.get(DATA_HOUSE_CATALOG)
    if catalog is None:
        catalog = HouseCatalog(hass)
        hass.data[DATA_HOUSE_CATALOG] = catalog
    return catalog
