"""The default system prompt, rendered against a real Home Assistant.

Checks what the model actually reads: the date and time in a readable form, no
"Current Area" when it is unknown, aliases without Home Assistant's
computed-name marker, and a device table whose rows have the header's columns.
"""

import csv
import io
import re

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import template

from custom_components.pepa_sensory_arm_house.const import DEFAULT_SYSTEM_PROMPT

HEADER = [
    "entity_id",
    "name",
    "state",
    "aliases",
    "area",
    "type",
    "current_value",
    "available_services",
]


@pytest.fixture
async def prompt(hass: HomeAssistant) -> str:
    """Render the default prompt with a light (aliases incl. the marker) and a sensor."""
    registry = er.async_get(hass)
    light = registry.async_get_or_create(
        "light", "test", "lamp", suggested_object_id="lamp", original_name="Lámpara Sala"
    )
    registry.async_update_entity(
        light.entity_id, aliases=[er.COMPUTED_NAME, "living room lamp", "la lámpara"]
    )
    hass.states.async_set(light.entity_id, "off", {"friendly_name": "Lámpara Sala"})
    hass.states.async_set("sensor.room_temp", "79.7", {"friendly_name": "Room Temp"})

    # What the agent passes: aliases filtered the way get_exposed_entities does.
    entry = registry.async_get(light.entity_id)
    exposed = [
        {
            "entity_id": light.entity_id,
            "name": "Lámpara Sala",
            "state": "off",
            "aliases": [a for a in entry.aliases if isinstance(a, str) and a.strip()],
        },
        {"entity_id": "sensor.room_temp", "name": "Room Temp", "state": "79.7", "aliases": []},
    ]
    return template.Template(DEFAULT_SYSTEM_PROMPT, hass).async_render(
        {"exposed_entities": exposed, "current_device_id": None}
    )


def _table(prompt: str) -> list[list[str]]:
    start = prompt.index("```csv") + len("```csv")
    body = prompt[start : prompt.index("```", start)].strip().splitlines()
    rows = [line for line in body if "," in line]
    return list(csv.reader(io.StringIO("\n".join(rows))))


async def test_date_and_time_is_readable(prompt: str) -> None:
    """Weekday, day, month, year and time, without seconds or offset."""
    line = next(ln for ln in prompt.splitlines() if ln.startswith("Current Date and Time: "))
    assert re.fullmatch(
        r"Current Date and Time: [A-Z][a-z]+day \d{1,2} [A-Z][a-z]+ \d{4}, \d{1,2}:\d{2} [AP]M",
        line,
    ), line
    assert "Never say you have no clock" in prompt


async def test_no_area_line_when_unknown(prompt: str) -> None:
    assert "Current Area" not in prompt
    assert "None" not in prompt.split("## Current Home Context")[1].split("```csv")[0]


async def test_aliases_carry_no_marker(prompt: str) -> None:
    assert "ComputedNameType" not in prompt
    assert "_singleton" not in prompt


async def test_every_row_has_the_header_columns(prompt: str) -> None:
    table = _table(prompt)
    assert table[0] == HEADER
    for row in table[1:]:
        assert len(row) == len(HEADER), row
    light = next(r for r in table if r[0] == "light.lamp")
    assert light[3] == "living room lamp|la lámpara"
    assert light[7].startswith("turn_on|turn_off|toggle")


async def test_sensors_are_read_only(prompt: str) -> None:
    sensor = next(r for r in _table(prompt) if r[0] == "sensor.room_temp")
    assert sensor[7] == "read_only"
