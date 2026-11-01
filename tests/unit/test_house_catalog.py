"""Unit tests for the house catalog (custom_components.pepa_sensory_arm_house.house_catalog)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.pepa_sensory_arm_house import house_catalog
from custom_components.pepa_sensory_arm_house.house_catalog import (
    CSV_COLUMNS,
    SENSOR_ENTITY_ID,
    HouseCatalog,
    async_get_house_catalog,
    build_catalog_csv,
)

SHOULD_EXPOSE = "homeassistant.components.homeassistant.exposed_entities.async_should_expose"
HEADER = ",".join(CSV_COLUMNS)


def _state(entity_id, friendly_name=None):
    attributes = {} if friendly_name is None else {"friendly_name": friendly_name}
    return SimpleNamespace(entity_id=entity_id, attributes=attributes)


def _entry(area_id=None, device_id=None, aliases=(), labels=()):
    return SimpleNamespace(
        area_id=area_id, device_id=device_id, aliases=set(aliases), labels=set(labels)
    )


class FakeHouse:
    """A small house: states, registry entries, devices, areas and exposure."""

    def __init__(self):
        self.states = {}
        self.entries = {}
        self.devices = {}
        self.areas = {}
        self.exposed = set()

        self.hass = MagicMock()
        self.hass.data = {}
        self.hass.states.async_all.side_effect = lambda: list(self.states.values())
        self.hass.states.get.side_effect = self._get_state

    def _get_state(self, entity_id):
        if entity_id == SENSOR_ENTITY_ID:
            return self.published
        return self.states.get(entity_id)

    published = None

    def add(self, entity_id, name, *, exposed=True, entry=None):
        self.states[entity_id] = _state(entity_id, name)
        if entry is not None:
            self.entries[entity_id] = entry
        if exposed:
            self.exposed.add(entity_id)

    def patches(self):
        entity_reg = SimpleNamespace(async_get=self.entries.get)
        device_reg = SimpleNamespace(async_get=self.devices.get)
        area_reg = SimpleNamespace(async_get_area=self.areas.get)
        return (
            patch.object(house_catalog.er, "async_get", return_value=entity_reg),
            patch.object(house_catalog.dr, "async_get", return_value=device_reg),
            patch.object(house_catalog.ar, "async_get", return_value=area_reg),
            patch(SHOULD_EXPOSE, side_effect=lambda _h, _a, eid: eid in self.exposed),
        )


@pytest.fixture
def house():
    house = FakeHouse()
    house.areas["sala"] = SimpleNamespace(name="Sala")
    house.areas["carport"] = SimpleNamespace(name="Carport")
    house.devices["dev_gate"] = SimpleNamespace(area_id="carport")
    house.add(
        "light.sala_techo",
        "Luz del techo",
        entry=_entry(area_id="sala", aliases=["luz de la sala", "techo"]),
    )
    house.add(
        "cover.porton",
        "Portón",
        entry=_entry(device_id="dev_gate", labels=["verify"]),
    )
    house.add(
        "binary_sensor.puerta",
        "Puerta, entrada",
        entry=_entry(labels=["volatile"]),
    )
    house.add("light.not_exposed", "Hidden", exposed=False, entry=_entry())
    return house


def _build(house):
    p1, p2, p3, p4 = house.patches()
    with p1, p2, p3, p4:
        return build_catalog_csv(house.hass, frozenset(house.exposed))


class TestBuildCatalogCsv:
    def test_header_and_exposed_rows_only(self, house):
        text, count = _build(house)
        lines = text.split("\n")
        assert lines[0] == HEADER
        assert count == 3
        assert len(lines) == 4
        assert not any(line.startswith("light.not_exposed") for line in lines)

    def test_rows_sorted_and_no_trailing_newline(self, house):
        text, _ = _build(house)
        ids = [line.split(",")[0] for line in text.split("\n")[1:]]
        assert ids == sorted(ids)
        assert not text.endswith("\n")

    def test_area_from_entity_aliases_sorted(self, house):
        text, _ = _build(house)
        row = next(r for r in text.split("\n") if r.startswith("light.sala_techo"))
        assert row == (
            "light.sala_techo,Luz del techo,Sala,luz de la sala|techo,light,"
            "turn_on|turn_off|toggle|turn_on[brightness_pct]|turn_on[rgb_color],false,false"
        )

    def test_area_from_device_and_verify_label(self, house):
        text, _ = _build(house)
        row = next(r for r in text.split("\n") if r.startswith("cover.porton"))
        assert row == (
            "cover.porton,Portón,Carport,,cover,"
            "open_cover|close_cover|set_cover_position|toggle,false,true"
        )

    def test_volatile_label_unassigned_area_and_quoting(self, house):
        text, _ = _build(house)
        row = next(r for r in text.split("\n") if r.startswith("binary_sensor.puerta"))
        assert row == (
            'binary_sensor.puerta,"Puerta, entrada",Unassigned,,binary_sensor,'
            "read_only,true,false"
        )

    def test_entity_area_overrides_device_area(self, house):
        house.add(
            "light.portal",
            "Portal",
            entry=_entry(area_id="sala", device_id="dev_gate"),
        )
        text, _ = _build(house)
        row = next(r for r in text.split("\n") if r.startswith("light.portal"))
        assert row.split(",")[2] == "Sala"

    def test_non_text_aliases_are_dropped(self, house):
        marker = object()
        house.entries["light.sala_techo"].aliases.add(marker)
        text, _ = _build(house)
        row = next(r for r in text.split("\n") if r.startswith("light.sala_techo"))
        assert row.split(",")[3] == "luz de la sala|techo"

    def test_entity_without_registry_entry(self, house):
        house.add("light.yaml_only", "YAML light")
        text, _ = _build(house)
        row = next(r for r in text.split("\n") if r.startswith("light.yaml_only"))
        assert row.split(",")[2:4] == ["Unassigned", ""]

    def test_same_house_same_bytes(self, house):
        assert _build(house) == _build(house)


@pytest.fixture
def started(house):
    """A catalog with one config entry using it, lifecycle hooks stubbed."""
    p1, p2, p3, p4 = house.patches()
    listen = patch.object(
        house_catalog.exposed_entities, "async_listen_entity_updates", return_value=MagicMock()
    )
    at_started = patch.object(house_catalog, "async_at_started", return_value=MagicMock())
    debouncer_cls = patch.object(house_catalog, "Debouncer")

    def _publish(entity_id, state, attributes):
        house.published = SimpleNamespace(state=state, attributes=attributes)

    house.hass.states.async_set.side_effect = _publish
    house.hass.bus.async_listen.return_value = MagicMock()

    with p1, p2, p3, p4, listen as listen_mock, at_started, debouncer_cls:
        catalog = HouseCatalog(house.hass)
        catalog.async_add_user("entry1")
        yield catalog, listen_mock


class TestHouseCatalogLifecycle:
    def test_without_users_answers_but_does_not_publish(self, house):
        p1, p2, p3, p4 = house.patches()
        with p1, p2, p3, p4:
            catalog = HouseCatalog(house.hass)
            exposed = catalog.async_refresh()
        assert exposed == frozenset({"light.sala_techo", "cover.porton", "binary_sensor.puerta"})
        house.hass.states.async_set.assert_not_called()

    def test_first_user_publishes_sensor(self, house, started):
        catalog, _ = started
        house.hass.states.async_set.assert_called_once()
        entity_id, state, attributes = house.hass.states.async_set.call_args.args
        assert entity_id == SENSOR_ENTITY_ID
        assert state == 3
        assert attributes["count"] == 3
        assert attributes["csv"] == catalog.csv
        assert attributes["csv"].startswith(HEADER)

    def test_unchanged_house_is_not_republished(self, house, started):
        catalog, _ = started
        catalog.async_refresh()
        catalog._async_mark_changed()
        catalog.async_refresh()
        house.hass.states.async_set.assert_called_once()

    def test_late_entity_is_published_on_next_refresh(self, house, started):
        catalog, _ = started
        house.add("fan.cuarto", "Abanico", entry=_entry())
        exposed = catalog.async_refresh()
        assert "fan.cuarto" in exposed
        assert house.hass.states.async_set.call_count == 2
        assert "fan.cuarto" in catalog.csv

    def test_label_change_is_published_after_change_flag(self, house, started):
        catalog, _ = started
        house.entries["light.sala_techo"].labels.add("verify")
        catalog.async_refresh()  # same exposed set, nothing flagged: no rebuild
        assert house.hass.states.async_set.call_count == 1
        catalog._async_mark_changed()  # what a registry event does
        catalog.async_refresh()  # what the debouncer then runs
        assert house.hass.states.async_set.call_count == 2
        row = next(r for r in catalog.csv.split("\n") if r.startswith("light.sala_techo"))
        assert row.endswith(",true")

    def test_listens_for_exposure_and_registry_changes(self, house, started):
        _, listen_mock = started
        listen_mock.assert_called_once()
        assert listen_mock.call_args.args[1] == "conversation"
        events = {call.args[0] for call in house.hass.bus.async_listen.call_args_list}
        assert events == {
            house_catalog.er.EVENT_ENTITY_REGISTRY_UPDATED,
            house_catalog.dr.EVENT_DEVICE_REGISTRY_UPDATED,
            house_catalog.ar.EVENT_AREA_REGISTRY_UPDATED,
        }

    def test_last_user_stops_and_removes_sensor(self, house, started):
        catalog, listen_mock = started
        unsubscribe = listen_mock.return_value
        catalog.async_remove_user("entry1")
        unsubscribe.assert_called_once()
        house.hass.states.async_remove.assert_called_once_with(SENSOR_ENTITY_ID)
        assert not catalog.active

    def test_other_user_keeps_it_running(self, house, started):
        catalog, listen_mock = started
        catalog.async_add_user("entry2")
        listen_mock.assert_called_once()  # not started twice
        catalog.async_remove_user("entry1")
        house.hass.states.async_remove.assert_not_called()
        assert catalog.active


def test_get_house_catalog_is_shared():
    hass = MagicMock()
    hass.data = {}
    assert async_get_house_catalog(hass) is async_get_house_catalog(hass)
