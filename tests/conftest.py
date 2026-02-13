"""Shared fixtures for thermostat_proxy tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import CONF_NAME, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component

from custom_components.thermostat_proxy.climate import (
    CustomThermostatEntity,
    TrackableSetting,
    _CORE_TRACKED_SETTINGS,
)

from custom_components.thermostat_proxy.const import (
    CONF_COOLDOWN_PERIOD,
    CONF_DEFAULT_SENSOR,
    CONF_IT_SETTINGS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_PHYSICAL_SENSOR_NAME,
    CONF_SENSOR_ENTITY_ID,
    CONF_SENSOR_NAME,
    CONF_SENSORS,
    CONF_SSOT_SETTINGS,
    CONF_THERMOSTAT,
    CONF_UNIQUE_ID,
    CONF_USE_LAST_ACTIVE_SENSOR,
    DOMAIN,
)

# ── Auto-mock logbook so the integration can load ─────────────────────


@pytest.fixture(autouse=True)
async def mock_logbook(hass: HomeAssistant) -> None:
    """Prevent the real logbook from loading (it needs frontend/recorder)."""
    hass.config.components.add("logbook")


# ── Default thermostat attributes ──────────────────────────────────────

DEFAULT_SUPPORTED_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.SWING_MODE
)

DEFAULT_THERMOSTAT_ATTRS: dict[str, Any] = {
    "temperature": 22.0,
    "current_temperature": 21.0,
    "hvac_modes": [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL],
    "hvac_mode": HVACMode.HEAT,
    "supported_features": DEFAULT_SUPPORTED_FEATURES,
    "fan_mode": "auto",
    "fan_modes": ["auto", "low", "high"],
    "swing_mode": "off",
    "swing_modes": ["off", "on"],
    "target_temp_step": 0.5,
    "min_temp": 5.0,
    "max_temp": 35.0,
}

REAL_THERMOSTAT_ENTITY = "climate.real_thermostat"
SENSOR_ENTITY = "sensor.bedroom_temp"
DEFAULT_SENSOR_TEMP = "23.5"


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def base_config_data() -> dict[str, Any]:
    """Standard config entry data with one sensor, no SSOT/IT."""
    return {
        CONF_NAME: "Test Proxy",
        CONF_THERMOSTAT: REAL_THERMOSTAT_ENTITY,
        CONF_UNIQUE_ID: "test_proxy_uid",
        CONF_SENSORS: [
            {CONF_SENSOR_NAME: "Bedroom", CONF_SENSOR_ENTITY_ID: SENSOR_ENTITY},
        ],
        CONF_PHYSICAL_SENSOR_NAME: "Physical Entity",
        CONF_DEFAULT_SENSOR: "Bedroom",
        CONF_USE_LAST_ACTIVE_SENSOR: False,
        CONF_COOLDOWN_PERIOD: 0,
        CONF_MIN_TEMP: None,
        CONF_MAX_TEMP: None,
        CONF_SSOT_SETTINGS: [],
        CONF_IT_SETTINGS: [],
    }


@pytest.fixture
def ssot_config_data(base_config_data: dict[str, Any]) -> dict[str, Any]:
    """Config with SSOT enabled for all core + supported settings."""
    return {
        **base_config_data,
        CONF_SSOT_SETTINGS: ["hvac_mode", "temperature", "fan_mode", "swing_mode"],
    }


@pytest.fixture
def it_config_data(base_config_data: dict[str, Any]) -> dict[str, Any]:
    """Config with IT enabled for hvac_mode and temperature."""
    return {
        **base_config_data,
        CONF_IT_SETTINGS: ["hvac_mode", "temperature"],
    }


def make_thermostat_state(
    state: str = HVACMode.HEAT,
    **attr_overrides: Any,
) -> State:
    """Create a mock physical thermostat State object."""
    attrs = {**DEFAULT_THERMOSTAT_ATTRS, **attr_overrides}
    return State(REAL_THERMOSTAT_ENTITY, state, attrs)


def make_sensor_state(
    entity_id: str = SENSOR_ENTITY,
    temperature: str = DEFAULT_SENSOR_TEMP,
) -> State:
    """Create a mock temperature sensor State object."""
    return State(
        entity_id,
        temperature,
        {
            "unit_of_measurement": UnitOfTemperature.CELSIUS,
            "device_class": "temperature",
        },
    )


@pytest.fixture
def make_entity(hass: HomeAssistant):
    """Factory fixture: build a CustomThermostatEntity for unit-level tests.

    Returns a callable that accepts keyword overrides for the constructor.
    The entity is NOT added to hass; use it for direct method testing.
    """

    def _make(**overrides: Any) -> CustomThermostatEntity:
        defaults: dict[str, Any] = {
            "hass": hass,
            "name": "Test Proxy",
            "real_thermostat": REAL_THERMOSTAT_ENTITY,
            "sensors": [
                {CONF_SENSOR_NAME: "Bedroom", CONF_SENSOR_ENTITY_ID: SENSOR_ENTITY},
            ],
            "default_sensor": "Bedroom",
            "unique_id": "test_proxy_uid",
            "physical_sensor_name": "Physical Entity",
            "use_last_active_sensor": False,
            "cooldown_period": 0,
            "user_min_temp": None,
            "user_max_temp": None,
            "single_source_of_truth": False,
            "ignore_thermostat": False,
            "ssot_settings": None,
            "it_settings": None,
        }
        defaults.update(overrides)
        entity = CustomThermostatEntity(**defaults)
        return entity

    return _make


# ── Shared test helpers ────────────────────────────────────────────────


def make_simple_state(
    hvac: str = "heat",
    temperature: float = 22.0,
    fan_mode: str | None = "auto",
    swing_mode: str | None = "off",
    **extra: Any,
) -> State:
    """Build a physical thermostat State for SSOT/echo/validation tests."""
    attrs: dict[str, Any] = {"temperature": temperature, **extra}
    if fan_mode is not None:
        attrs["fan_mode"] = fan_mode
    if swing_mode is not None:
        attrs["swing_mode"] = swing_mode
    return State(REAL_THERMOSTAT_ENTITY, hvac, attrs)


def seed_core_baselines(
    entity: CustomThermostatEntity,
    hvac: str = "heat",
    temperature: float = 22.0,
    fan_mode: str = "auto",
    swing_mode: str = "off",
) -> None:
    """Seed the 4 core SSOT baselines and active tracked settings."""
    entity._ssot_baselines[TrackableSetting.HVAC_MODE] = hvac
    entity._last_real_target_temp = temperature
    entity._ssot_baselines[TrackableSetting.FAN_MODE] = fan_mode
    entity._ssot_baselines[TrackableSetting.SWING_MODE] = swing_mode
    entity._active_tracked_settings = {
        TrackableSetting.HVAC_MODE,
        TrackableSetting.TEMPERATURE,
        TrackableSetting.FAN_MODE,
        TrackableSetting.SWING_MODE,
    }


def get_service_call_data(mock_call: Any, key: str | None = None) -> Any:
    """Extract service call data dict from a patched ServiceRegistry.async_call."""
    args = mock_call.call_args[0]
    data = next(a for a in args if isinstance(a, dict))
    return data.get(key) if key is not None else data
