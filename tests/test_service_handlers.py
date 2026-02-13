"""Tests for user-facing service call handlers.

Exercises async_set_temperature, async_set_hvac_mode, async_set_fan_mode,
async_set_preset_mode by constructing entities via make_entity and patching
ServiceRegistry.async_call at the class level (slots prevent instance patching).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceRegistry, State

from custom_components.thermostat_proxy.climate import TrackableSetting

from .conftest import (
    DEFAULT_THERMOSTAT_ATTRS,
    REAL_THERMOSTAT_ENTITY,
    SENSOR_ENTITY,
    make_thermostat_state,
    make_sensor_state,
)

PATCH_ASYNC_CALL = "homeassistant.core.ServiceRegistry.async_call"


@pytest.fixture
def entity(hass: HomeAssistant, make_entity):
    """Entity pre-seeded with real thermostat state and sensor state.

    Real thermostat: current_temperature=21.0, target=22.0
    Sensor: 23.5
    Virtual target: 24.0 (so delta = 24.0 - 23.5 = 0.5)
    """
    ent = make_entity()
    # Set real thermostat state in hass and on entity
    real_state = make_thermostat_state()
    hass.states.async_set(
        REAL_THERMOSTAT_ENTITY,
        real_state.state,
        real_state.attributes,
    )
    ent._real_state = hass.states.get(REAL_THERMOSTAT_ENTITY)

    # Set sensor state
    sensor_state = make_sensor_state()
    hass.states.async_set(
        SENSOR_ENTITY,
        sensor_state.state,
        sensor_state.attributes,
    )
    ent._sensor_states[SENSOR_ENTITY] = hass.states.get(SENSOR_ENTITY)

    # Pre-seed virtual target and real target
    ent._virtual_target_temperature = 24.0
    ent._last_real_target_temp = 22.0
    # Pre-seed temp limits from real thermostat
    ent._real_min_temp = 5.0
    ent._real_max_temp = 35.0
    ent._target_temp_step = 0.5

    return ent


# ── async_set_temperature ────────────────────────────────────────────


class TestSetTemperature:
    """async_set_temperature delta computation and side-effects."""

    async def test_set_temperature_delta_computation(
        self, hass: HomeAssistant, entity
    ) -> None:
        """Target = 25 → delta = 25 - 23.5 = 1.5 → real = 21 + 1.5 = 22.5."""
        entity._async_log_real_adjustment = AsyncMock()
        entity.async_write_ha_state = lambda: None

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            await entity.async_set_temperature(temperature=25.0)

            mock_call.assert_called_once()
            call_args = mock_call.call_args
            # Find the service data dict in the positional args
            data = next(
                a for a in call_args[0]
                if isinstance(a, dict) and ATTR_TEMPERATURE in a
            )
            assert data[ATTR_TEMPERATURE] == 22.5
            assert data[ATTR_ENTITY_ID] == REAL_THERMOSTAT_ENTITY

    async def test_set_temperature_safety_clamp(
        self, hass: HomeAssistant, entity
    ) -> None:
        """When real_target exceeds max, it should be clamped."""
        entity._async_log_real_adjustment = AsyncMock()
        entity.async_write_ha_state = lambda: None

        # Set user_max_temp to 25.0
        entity._user_max_temp = 25.0

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            # Request a very high virtual temp: 50 → delta = 50-23.5=26.5 → real=21+26.5=47.5
            # But safety clamp should cap to 25.0
            await entity.async_set_temperature(temperature=50.0)

            mock_call.assert_called_once()
            data = next(
                a for a in mock_call.call_args[0]
                if isinstance(a, dict) and ATTR_TEMPERATURE in a
            )
            assert data[ATTR_TEMPERATURE] <= 25.0

    async def test_set_temperature_records_pending(
        self, hass: HomeAssistant, entity
    ) -> None:
        """After set_temperature, a pending TEMPERATURE request exists."""
        entity._async_log_real_adjustment = AsyncMock()
        entity.async_write_ha_state = lambda: None

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await entity.async_set_temperature(temperature=25.0)

        # Should have recorded a pending request for the real target
        assert entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 22.5, tolerance=0.5
        )

    async def test_set_temperature_rollback_on_failure(
        self, hass: HomeAssistant, entity
    ) -> None:
        """Service call failure should rollback _last_real_target_temp."""
        original_real_target = entity._last_real_target_temp  # 22.0
        entity._async_log_real_adjustment = AsyncMock()
        entity.async_write_ha_state = lambda: None

        with patch(
            PATCH_ASYNC_CALL,
            side_effect=RuntimeError("Service call failed"),
        ):
            with pytest.raises(RuntimeError, match="Service call failed"):
                await entity.async_set_temperature(temperature=25.0)

        # _last_real_target_temp should be rolled back to original
        assert entity._last_real_target_temp == original_real_target

    async def test_set_temperature_updates_virtual_target(
        self, hass: HomeAssistant, entity
    ) -> None:
        """After success, _virtual_target_temperature should be updated."""
        entity._async_log_real_adjustment = AsyncMock()
        entity.async_write_ha_state = lambda: None

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await entity.async_set_temperature(temperature=25.0)

        assert entity._virtual_target_temperature == 25.0


# ── async_set_hvac_mode ──────────────────────────────────────────────


class TestSetHvacMode:
    """async_set_hvac_mode updates baseline and records pending."""

    async def test_set_hvac_mode_forwards_call(
        self, hass: HomeAssistant, entity
    ) -> None:
        """Service call forwarded with correct payload."""
        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            await entity.async_set_hvac_mode(HVACMode.COOL)

            mock_call.assert_called_once()
            call_args = mock_call.call_args
            # Patching class method: args are (self, domain, service, data, ...)
            # self is args[0][0], domain args[0][1], service args[0][2], data args[0][3]
            # but let's just check kwargs or positional for the data dict
            # Actually: async_call(domain, service, data, blocking=True)
            # When patching class method, first arg is self, so positional[1]=domain, [2]=service, [3]=data
            assert any(
                isinstance(a, dict) and a.get("hvac_mode") == HVACMode.COOL
                for a in call_args[0]
            )

    @pytest.mark.parametrize(
        "from_mode,to_mode",
        [
            (HVACMode.COOL, HVACMode.OFF),
            (HVACMode.OFF, HVACMode.COOL),
            (HVACMode.HEAT, HVACMode.OFF),
            (HVACMode.OFF, HVACMode.HEAT),
        ],
    )
    async def test_set_hvac_mode_forwards_to_real_device(
        self, hass: HomeAssistant, make_entity, from_mode: HVACMode, to_mode: HVACMode
    ) -> None:
        """Setting HVAC mode calls climate.set_hvac_mode on the physical entity."""
        ent = make_entity()
        real_state = make_thermostat_state(state=from_mode)
        hass.states.async_set(
            REAL_THERMOSTAT_ENTITY, real_state.state, real_state.attributes,
        )
        ent._real_state = hass.states.get(REAL_THERMOSTAT_ENTITY)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            await ent.async_set_hvac_mode(to_mode)

            mock_call.assert_called_once()
            # async_call is patched at class level, so positional args are:
            # (self, domain, service, data, ...)
            args = mock_call.call_args[0]
            # Find domain and service (strings that aren't 'self')
            str_args = [a for a in args if isinstance(a, str)]
            assert "climate" in str_args
            assert "set_hvac_mode" in str_args
            # Find the data dict
            data = next(a for a in args if isinstance(a, dict))
            assert data[ATTR_ENTITY_ID] == REAL_THERMOSTAT_ENTITY
            assert data["hvac_mode"] == to_mode

    async def test_set_hvac_mode_records_pending(
        self, hass: HomeAssistant, entity
    ) -> None:
        """A pending HVAC_MODE request should be recorded."""
        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await entity.async_set_hvac_mode(HVACMode.COOL)

        assert entity._has_pending_setting_request(
            TrackableSetting.HVAC_MODE, HVACMode.COOL
        )

    async def test_set_hvac_mode_updates_baseline_when_ssot(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        """With SSOT enabled, baselines should be updated."""
        ent = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
        )
        ent._ssot_baselines[TrackableSetting.HVAC_MODE] = "heat"

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_hvac_mode(HVACMode.COOL)

        assert ent._ssot_baselines[TrackableSetting.HVAC_MODE] == HVACMode.COOL


# ── async_set_fan_mode ───────────────────────────────────────────────


class TestSetFanMode:
    """async_set_fan_mode forwards and records pending."""

    async def test_set_fan_mode_records_pending(
        self, hass: HomeAssistant, entity
    ) -> None:
        """A pending FAN_MODE request should be recorded."""
        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await entity.async_set_fan_mode("high")

        assert entity._has_pending_setting_request(
            TrackableSetting.FAN_MODE, "high"
        )

    async def test_set_fan_mode_updates_baseline_when_ssot(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        """With SSOT, baselines should be updated."""
        ent = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
        )
        ent._ssot_baselines[TrackableSetting.FAN_MODE] = "auto"

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_fan_mode("high")

        assert ent._ssot_baselines[TrackableSetting.FAN_MODE] == "high"


# ── async_set_preset_mode ────────────────────────────────────────────


class TestSetPresetMode:
    """async_set_preset_mode switches sensor selection."""

    async def test_set_preset_mode_switches_sensor(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        """Selecting a valid preset should change _selected_sensor_name."""
        ent = make_entity(
            sensors=[
                {"name": "Bedroom", "entity_id": SENSOR_ENTITY},
                {"name": "Kitchen", "entity_id": "sensor.kitchen_temp"},
            ],
        )
        # Set up required state for preset mode to succeed
        real_state = make_thermostat_state()
        hass.states.async_set(
            REAL_THERMOSTAT_ENTITY,
            real_state.state,
            real_state.attributes,
        )
        ent._real_state = hass.states.get(REAL_THERMOSTAT_ENTITY)

        # Set sensor states
        sensor_state = make_sensor_state()
        hass.states.async_set(
            SENSOR_ENTITY, sensor_state.state, sensor_state.attributes,
        )
        ent._sensor_states[SENSOR_ENTITY] = hass.states.get(SENSOR_ENTITY)

        hass.states.async_set(
            "sensor.kitchen_temp", "22.0",
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        )
        ent._sensor_states["sensor.kitchen_temp"] = hass.states.get(
            "sensor.kitchen_temp"
        )

        ent._virtual_target_temperature = 24.0
        ent._last_real_target_temp = 22.0
        ent._real_min_temp = 5.0
        ent._real_max_temp = 35.0
        ent._target_temp_step = 0.5

        # Mock the realign call and logbook
        ent._async_realign_real_target_from_sensor = AsyncMock()
        ent.async_write_ha_state = lambda: None

        assert ent._selected_sensor_name == "Bedroom"

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_preset_mode("Kitchen")

        assert ent._selected_sensor_name == "Kitchen"

    async def test_set_preset_mode_invalid_raises(
        self, hass: HomeAssistant, entity
    ) -> None:
        """Selecting an unknown preset should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown preset"):
            await entity.async_set_preset_mode("Nonexistent Room")
