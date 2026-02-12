"""Tests for the thermostat_proxy config flow and options flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.thermostat_proxy.config_flow import (
    CustomThermostatConfigFlow,
    _build_available_settings,
    _migrate_bool_to_settings,
)
from custom_components.thermostat_proxy.const import (
    CONF_COOLDOWN_PERIOD,
    CONF_DEFAULT_SENSOR,
    CONF_IGNORE_THERMOSTAT,
    CONF_IT_SETTINGS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_PHYSICAL_SENSOR_NAME,
    CONF_SENSOR_ENTITY_ID,
    CONF_SENSOR_NAME,
    CONF_SENSORS,
    CONF_SINGLE_SOURCE_OF_TRUTH,
    CONF_SSOT_SETTINGS,
    CONF_THERMOSTAT,
    CONF_USE_LAST_ACTIVE_SENSOR,
    DOMAIN,
)
from .conftest import DEFAULT_SUPPORTED_FEATURES, REAL_THERMOSTAT_ENTITY


# ── Helper to drive through the multi-step config flow ─────────────────


async def _walk_flow_to_finalize(
    hass: HomeAssistant,
    *,
    name: str = "Test Proxy",
    thermostat: str = REAL_THERMOSTAT_ENTITY,
    sensor_name: str = "Bedroom",
    sensor_entity: str = "sensor.bedroom_temp",
) -> dict[str, Any]:
    """Walk the config flow from user step through to finalize, returning the
    result of the manage_sensors FINISH action (the finalize form)."""

    # Step 1: user
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"name": name, CONF_THERMOSTAT: thermostat},
    )

    # Step 2: manage_sensors → add a sensor
    assert result["step_id"] == "manage_sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "add_sensor"},
    )

    # Step 3: sensors → fill in sensor
    assert result["step_id"] == "sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SENSOR_NAME: sensor_name,
            CONF_SENSOR_ENTITY_ID: sensor_entity,
            "add_another": False,
        },
    )

    # Back to manage_sensors → finish
    assert result["step_id"] == "manage_sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "finish"},
    )

    # Now at finalize
    assert result["step_id"] == "finalize"
    return result


# ── Config flow tests ──────────────────────────────────────────────────


async def test_full_flow_creates_entry(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Walk the complete config flow and verify an entry is created."""
    # Set up the mock thermostat state for _build_available_settings
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await _walk_flow_to_finalize(hass)

    # Submit finalize with defaults
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHYSICAL_SENSOR_NAME: "Physical Entity",
            CONF_DEFAULT_SENSOR: "Bedroom",
            CONF_COOLDOWN_PERIOD: 0,
            CONF_MIN_TEMP: 0,
            CONF_MAX_TEMP: 0,
            CONF_SSOT_SETTINGS: [],
            CONF_IT_SETTINGS: [],
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Proxy"
    assert result["data"][CONF_THERMOSTAT] == REAL_THERMOSTAT_ENTITY
    assert len(result["data"][CONF_SENSORS]) == 1


async def test_sensor_reserved_name_error(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Using the reserved 'Physical Entity' name should produce an error."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"name": "Test", CONF_THERMOSTAT: REAL_THERMOSTAT_ENTITY},
    )
    # manage_sensors → add
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "add_sensor"},
    )
    # sensors → reserved name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SENSOR_NAME: "Physical Entity",
            CONF_SENSOR_ENTITY_ID: "sensor.temp",
            "add_another": False,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "reserved_sensor_name"


async def test_sensor_duplicate_name_error(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Adding a sensor with the same name as an existing one should error."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"name": "Test", CONF_THERMOSTAT: REAL_THERMOSTAT_ENTITY},
    )

    # Add first sensor
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "add_sensor"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SENSOR_NAME: "Bedroom",
            CONF_SENSOR_ENTITY_ID: "sensor.temp1",
            "add_another": False,
        },
    )

    # Try to add second sensor with same name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "add_sensor"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SENSOR_NAME: "Bedroom",
            CONF_SENSOR_ENTITY_ID: "sensor.temp2",
            "add_another": False,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "duplicate_sensor_name"


async def test_sensor_duplicate_entity_error(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Adding a sensor with the same entity_id should error."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"name": "Test", CONF_THERMOSTAT: REAL_THERMOSTAT_ENTITY},
    )

    # Add first sensor
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "add_sensor"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SENSOR_NAME: "Bedroom",
            CONF_SENSOR_ENTITY_ID: "sensor.temp1",
            "add_another": False,
        },
    )

    # Try second sensor with same entity
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "add_sensor"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SENSOR_NAME: "Living Room",
            CONF_SENSOR_ENTITY_ID: "sensor.temp1",
            "add_another": False,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "duplicate_sensor_entity"


async def test_manage_sensors_no_finish_without_sensors(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """When no sensors exist, 'finish' is not available as an action option."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"name": "Test", CONF_THERMOSTAT: REAL_THERMOSTAT_ENTITY},
    )

    # With no sensors, only "add_sensor" should be an available action
    assert result["step_id"] == "manage_sensors"
    schema_dict = dict(result["data_schema"].schema)
    # Find the action key's validator — it's a vol.In with the allowed options
    from custom_components.thermostat_proxy.config_flow import CONF_ACTION
    action_validator = None
    for key, validator in result["data_schema"].schema.items():
        if str(key) == CONF_ACTION:
            action_validator = validator
            break
    assert action_validator is not None
    # vol.In wraps a dict/list of allowed values — "finish" should NOT be there
    assert "finish" not in action_validator.container


async def test_finalize_invalid_temp_range(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """min_temp > max_temp should produce an error."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await _walk_flow_to_finalize(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHYSICAL_SENSOR_NAME: "Physical Entity",
            CONF_DEFAULT_SENSOR: "Bedroom",
            CONF_COOLDOWN_PERIOD: 0,
            CONF_MIN_TEMP: 30,
            CONF_MAX_TEMP: 20,
            CONF_SSOT_SETTINGS: [],
            CONF_IT_SETTINGS: [],
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_temp_range"


async def test_finalize_physical_name_conflict(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Physical sensor name matching an existing sensor should error."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await _walk_flow_to_finalize(hass, sensor_name="MyPhysical")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHYSICAL_SENSOR_NAME: "MyPhysical",  # conflicts!
            CONF_DEFAULT_SENSOR: "MyPhysical",
            CONF_COOLDOWN_PERIOD: 0,
            CONF_MIN_TEMP: 0,
            CONF_MAX_TEMP: 0,
            CONF_SSOT_SETTINGS: [],
            CONF_IT_SETTINGS: [],
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "physical_name_conflict"


async def test_finalize_saves_ssot_it_settings(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Verify SSOT and IT multi-select values are stored in the entry."""
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": DEFAULT_SUPPORTED_FEATURES},
    )

    result = await _walk_flow_to_finalize(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHYSICAL_SENSOR_NAME: "Physical Entity",
            CONF_DEFAULT_SENSOR: "Bedroom",
            CONF_COOLDOWN_PERIOD: 0,
            CONF_MIN_TEMP: 0,
            CONF_MAX_TEMP: 0,
            CONF_SSOT_SETTINGS: ["hvac_mode", "temperature"],
            CONF_IT_SETTINGS: ["fan_mode"],
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SSOT_SETTINGS] == ["hvac_mode", "temperature"]
    assert result["data"][CONF_IT_SETTINGS] == ["fan_mode"]


# ── _build_available_settings ──────────────────────────────────────────


def test_build_available_settings_probes_features(hass: HomeAssistant) -> None:
    """Device features should be reflected in available settings list."""
    features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TARGET_HUMIDITY
    )
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        "heat",
        {"supported_features": features},
    )

    options = _build_available_settings(hass, REAL_THERMOSTAT_ENTITY)
    values = [o["value"] for o in options]

    assert "hvac_mode" in values
    assert "temperature" in values
    assert "fan_mode" in values
    assert "swing_mode" in values
    assert "target_temp_high" in values
    assert "target_temp_low" in values
    assert "target_humidity" in values


def test_build_available_settings_minimal(hass: HomeAssistant) -> None:
    """Without a thermostat, only hvac_mode and temperature are returned."""
    options = _build_available_settings(hass, None)
    values = [o["value"] for o in options]
    assert values == ["hvac_mode", "temperature"]


# ── _migrate_bool_to_settings ─────────────────────────────────────────


class TestMigrateBoolToSettings:
    """Backward compat migration from old boolean config to per-setting lists."""

    def test_new_list_present_returned_unchanged(self) -> None:
        data = {CONF_SSOT_SETTINGS: ["hvac_mode"]}
        result = _migrate_bool_to_settings(
            data, {}, CONF_SINGLE_SOURCE_OF_TRUTH, CONF_SSOT_SETTINGS, ["a", "b"]
        )
        assert result == ["hvac_mode"]

    def test_old_boolean_true_returns_all(self) -> None:
        data = {CONF_SINGLE_SOURCE_OF_TRUTH: True}
        all_keys = ["hvac_mode", "temperature", "fan_mode"]
        result = _migrate_bool_to_settings(
            data, {}, CONF_SINGLE_SOURCE_OF_TRUTH, CONF_SSOT_SETTINGS, all_keys
        )
        assert result == all_keys

    def test_old_boolean_false_returns_empty(self) -> None:
        data = {CONF_SINGLE_SOURCE_OF_TRUTH: False}
        result = _migrate_bool_to_settings(
            data, {}, CONF_SINGLE_SOURCE_OF_TRUTH, CONF_SSOT_SETTINGS, ["a"]
        )
        assert result == []

    def test_no_key_at_all_returns_empty(self) -> None:
        result = _migrate_bool_to_settings(
            {}, {}, CONF_SINGLE_SOURCE_OF_TRUTH, CONF_SSOT_SETTINGS, ["a"]
        )
        assert result == []

    def test_options_take_precedence(self) -> None:
        data = {CONF_SSOT_SETTINGS: ["old_value"]}
        options = {CONF_SSOT_SETTINGS: ["from_options"]}
        result = _migrate_bool_to_settings(
            data, options, CONF_SINGLE_SOURCE_OF_TRUTH, CONF_SSOT_SETTINGS, ["a"]
        )
        assert result == ["from_options"]


# ── Options flow registration ────────────────────────────────────────


def test_options_flow_is_registered() -> None:
    """async_get_options_flow must be a method on the ConfigFlow class."""
    assert hasattr(CustomThermostatConfigFlow, "async_get_options_flow")
    assert callable(CustomThermostatConfigFlow.async_get_options_flow)
