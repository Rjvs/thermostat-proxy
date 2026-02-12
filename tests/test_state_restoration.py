"""Tests for state restoration (_async_restore_state)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant, State

from custom_components.thermostat_proxy.climate import TrackableSetting
from custom_components.thermostat_proxy.const import (
    ATTR_ACTIVE_SENSOR,
    ATTR_REAL_TARGET_TEMPERATURE,
    ATTR_SSOT_FAN_MODE,
    ATTR_SSOT_HVAC_MODE,
    ATTR_SSOT_SWING_MODE,
)
from homeassistant.const import ATTR_TEMPERATURE

from .conftest import REAL_THERMOSTAT_ENTITY


def _last_state(
    active_sensor: str | None = None,
    temperature: float | None = None,
    real_target: float | None = None,
    ssot_hvac: str | None = None,
    ssot_fan: str | None = None,
    ssot_swing: str | None = None,
) -> State:
    """Create a mock last-state for restoration."""
    attrs: dict = {}
    if active_sensor is not None:
        attrs[ATTR_ACTIVE_SENSOR] = active_sensor
    if temperature is not None:
        attrs[ATTR_TEMPERATURE] = temperature
    if real_target is not None:
        attrs[ATTR_REAL_TARGET_TEMPERATURE] = real_target
    if ssot_hvac is not None:
        attrs[ATTR_SSOT_HVAC_MODE] = ssot_hvac
    if ssot_fan is not None:
        attrs[ATTR_SSOT_FAN_MODE] = ssot_fan
    if ssot_swing is not None:
        attrs[ATTR_SSOT_SWING_MODE] = ssot_swing
    return State("climate.test_proxy", "heat", attrs)


class TestRestoreSensorSelection:
    """Sensor selection restoration hierarchy."""

    async def test_restore_last_active_sensor(self, hass, make_entity) -> None:
        entity = make_entity(use_last_active_sensor=True)
        last = _last_state(active_sensor="Bedroom")
        with patch.object(entity, "async_get_last_state", return_value=last):
            await entity._async_restore_state()
        assert entity._selected_sensor_name == "Bedroom"

    async def test_restore_falls_back_to_configured_default(
        self, hass, make_entity
    ) -> None:
        """When use_last_active is False, configured default wins."""
        entity = make_entity(
            use_last_active_sensor=False,
            default_sensor="Bedroom",
        )
        last = _last_state(active_sensor="Living Room")  # Not in sensor list
        with patch.object(entity, "async_get_last_state", return_value=last):
            await entity._async_restore_state()
        # Configured default should be used since "Living Room" is not in lookup
        assert entity._selected_sensor_name == "Bedroom"


class TestRestoreTemperatures:
    """Virtual and real target temperature restoration."""

    async def test_restore_virtual_target(self, hass, make_entity) -> None:
        entity = make_entity()
        last = _last_state(temperature=24.5)
        with patch.object(entity, "async_get_last_state", return_value=last):
            await entity._async_restore_state()
        # _apply_target_constraints may adjust, but with no limits set
        # the value should pass through
        assert entity._virtual_target_temperature == 24.5

    async def test_restore_real_target(self, hass, make_entity) -> None:
        entity = make_entity()
        last = _last_state(real_target=23.0)
        with patch.object(entity, "async_get_last_state", return_value=last):
            await entity._async_restore_state()
        assert entity._last_real_target_temp == 23.0


class TestRestoreSSOTBaselines:
    """SSOT baseline restoration."""

    async def test_restore_ssot_baselines(self, hass, make_entity) -> None:
        entity = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
        )
        last = _last_state(
            ssot_hvac="cool",
            ssot_fan="high",
            ssot_swing="on",
        )
        with patch.object(entity, "async_get_last_state", return_value=last):
            await entity._async_restore_state()
        assert entity._ssot_hvac_mode == "cool"
        assert entity._ssot_fan_mode == "high"
        assert entity._ssot_swing_mode == "on"

    async def test_no_restore_when_ssot_disabled(self, hass, make_entity) -> None:
        entity = make_entity()  # No SSOT
        last = _last_state(ssot_hvac="cool", ssot_fan="high")
        with patch.object(entity, "async_get_last_state", return_value=last):
            await entity._async_restore_state()
        # Baselines should remain None when SSOT is off
        assert entity._ssot_hvac_mode is None
        assert entity._ssot_fan_mode is None
