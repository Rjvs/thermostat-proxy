"""Tests verifying the proxy faithfully mirrors the physical device's ClimateEntity interface.

Ensures supported_features, properties, service handlers, and extra_state_attributes
all reflect the real thermostat accurately without double-publishing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, State

from custom_components.thermostat_proxy.climate import (
    CustomThermostatEntity,
    TrackableSetting,
    _RESERVED_REAL_ATTRIBUTES,
)

from .conftest import (
    DEFAULT_THERMOSTAT_ATTRS,
    REAL_THERMOSTAT_ENTITY,
    SENSOR_ENTITY,
    make_sensor_state,
    make_thermostat_state,
)

PATCH_ASYNC_CALL = "homeassistant.core.ServiceRegistry.async_call"

# A maximally-featured thermostat exposing every ClimateEntity capability.
FULL_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.TARGET_HUMIDITY
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.PRESET_MODE
    | ClimateEntityFeature.SWING_MODE
    | ClimateEntityFeature.SWING_HORIZONTAL_MODE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)

FULL_THERMOSTAT_ATTRS: dict[str, Any] = {
    "temperature": 22.0,
    "current_temperature": 21.0,
    "target_temp_high": 25.0,
    "target_temp_low": 18.0,
    "hvac_modes": [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL],
    "hvac_mode": HVACMode.HEAT,
    "hvac_action": "heating",
    "supported_features": FULL_FEATURES,
    "fan_mode": "auto",
    "fan_modes": ["auto", "low", "medium", "high"],
    "swing_mode": "off",
    "swing_modes": ["off", "vertical"],
    "swing_horizontal_mode": "off",
    "swing_horizontal_modes": ["off", "horizontal"],
    "target_temp_step": 0.5,
    "min_temp": 5.0,
    "max_temp": 35.0,
    "humidity": 45,
    "current_humidity": 50,
    "min_humidity": 20,
    "max_humidity": 80,
    "precision": 0.1,
}


def _make_full_state(**overrides: Any) -> State:
    """Create a full-featured thermostat State."""
    attrs = {**FULL_THERMOSTAT_ATTRS, **overrides}
    return State(REAL_THERMOSTAT_ENTITY, str(HVACMode.HEAT), attrs)


def _seed_entity(
    hass: HomeAssistant,
    ent: CustomThermostatEntity,
    state: State | None = None,
) -> None:
    """Seed an entity with a real thermostat state and sensor state."""
    real = state or _make_full_state()
    hass.states.async_set(REAL_THERMOSTAT_ENTITY, real.state, real.attributes)
    ent._real_state = hass.states.get(REAL_THERMOSTAT_ENTITY)
    ent._update_real_temperature_limits()

    sensor = make_sensor_state()
    hass.states.async_set(SENSOR_ENTITY, sensor.state, sensor.attributes)
    ent._sensor_states[SENSOR_ENTITY] = hass.states.get(SENSOR_ENTITY)

    ent._virtual_target_temperature = 24.0
    ent._last_real_target_temp = 22.0


# ── Supported features mirroring ──────────────────────────────────────


class TestSupportedFeaturesMirroring:
    """Proxy's supported_features must mirror the real device (plus PRESET_MODE)."""

    def test_full_features_mirrored(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)

        features = ent.supported_features
        assert features & ClimateEntityFeature.TARGET_TEMPERATURE
        assert features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        assert features & ClimateEntityFeature.TARGET_HUMIDITY
        assert features & ClimateEntityFeature.FAN_MODE
        assert features & ClimateEntityFeature.SWING_MODE
        assert features & ClimateEntityFeature.SWING_HORIZONTAL_MODE
        assert features & ClimateEntityFeature.TURN_ON
        assert features & ClimateEntityFeature.TURN_OFF
        assert features & ClimateEntityFeature.PRESET_MODE  # proxy always adds

    def test_minimal_features(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        minimal = make_thermostat_state(
            supported_features=ClimateEntityFeature.TARGET_TEMPERATURE,
        )
        _seed_entity(hass, ent, minimal)

        features = ent.supported_features
        assert features & ClimateEntityFeature.TARGET_TEMPERATURE
        assert features & ClimateEntityFeature.PRESET_MODE
        assert not (features & ClimateEntityFeature.FAN_MODE)
        assert not (features & ClimateEntityFeature.SWING_MODE)
        assert not (features & ClimateEntityFeature.SWING_HORIZONTAL_MODE)
        assert not (features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)
        assert not (features & ClimateEntityFeature.TARGET_HUMIDITY)
        assert not (features & ClimateEntityFeature.TURN_ON)
        assert not (features & ClimateEntityFeature.TURN_OFF)

    @pytest.mark.parametrize(
        "flag",
        [
            ClimateEntityFeature.FAN_MODE,
            ClimateEntityFeature.SWING_MODE,
            ClimateEntityFeature.SWING_HORIZONTAL_MODE,
            ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
            ClimateEntityFeature.TARGET_HUMIDITY,
            ClimateEntityFeature.TURN_ON,
            ClimateEntityFeature.TURN_OFF,
        ],
    )
    def test_individual_feature_forwarded(
        self, hass: HomeAssistant, make_entity, flag: ClimateEntityFeature
    ) -> None:
        ent = make_entity()
        state = make_thermostat_state(
            supported_features=ClimateEntityFeature.TARGET_TEMPERATURE | flag,
        )
        _seed_entity(hass, ent, state)
        assert ent.supported_features & flag


# ── Property forwarding ───────────────────────────────────────────────


class TestPropertyForwarding:
    """Forwarded properties must reflect the real device's values."""

    def test_hvac_action(self, hass: HomeAssistant, make_entity) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.hvac_action == HVACAction.HEATING

    def test_current_humidity(self, hass: HomeAssistant, make_entity) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.current_humidity == 50

    def test_target_humidity(self, hass: HomeAssistant, make_entity) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.target_humidity == 45

    def test_min_humidity(self, hass: HomeAssistant, make_entity) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.min_humidity == 20

    def test_max_humidity(self, hass: HomeAssistant, make_entity) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.max_humidity == 80

    def test_min_humidity_default_when_absent(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        state = make_thermostat_state()  # no min_humidity attribute
        _seed_entity(hass, ent, state)
        assert ent.min_humidity == 30  # ClimateEntity default

    def test_max_humidity_default_when_absent(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        state = make_thermostat_state()  # no max_humidity attribute
        _seed_entity(hass, ent, state)
        assert ent.max_humidity == 99  # ClimateEntity default

    def test_target_temperature_high(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.target_temperature_high == 25.0

    def test_target_temperature_low(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.target_temperature_low == 18.0

    def test_swing_horizontal_mode(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.swing_horizontal_mode == "off"

    def test_swing_horizontal_modes(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        assert ent.swing_horizontal_modes == ["off", "horizontal"]

    def test_all_none_when_no_real_state(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        ent._real_state = None
        assert ent.hvac_action is None
        assert ent.current_humidity is None
        assert ent.target_humidity is None
        assert ent.target_temperature_high is None
        assert ent.target_temperature_low is None
        assert ent.swing_horizontal_mode is None
        assert ent.swing_horizontal_modes is None


# ── No double-publishing ──────────────────────────────────────────────


class TestNoDoublePublishing:
    """extra_state_attributes must not contain any key that HA publishes via properties."""

    def test_reserved_attrs_excluded(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)
        attrs = ent.extra_state_attributes

        for key in _RESERVED_REAL_ATTRIBUTES:
            assert key not in attrs, (
                f"Reserved key '{key}' leaked into extra_state_attributes"
            )


# ── Service forwarding ────────────────────────────────────────────────


class TestServiceForwarding:
    """New service handlers forward to the real device correctly."""

    async def test_set_swing_horizontal_mode_forwards(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            await ent.async_set_swing_horizontal_mode("horizontal")

            mock_call.assert_called_once()
            args = mock_call.call_args[0]
            assert any(
                isinstance(a, dict) and a.get("swing_horizontal_mode") == "horizontal"
                for a in args
            )

    async def test_set_swing_horizontal_mode_records_pending(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_swing_horizontal_mode("horizontal")

        assert ent._has_pending_setting_request(
            TrackableSetting.SWING_HORIZONTAL_MODE, "horizontal"
        )

    async def test_set_swing_horizontal_mode_updates_ssot_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode", "swing_horizontal_mode"],
        )
        ent._ssot_baselines[TrackableSetting.SWING_HORIZONTAL_MODE] = "off"
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_swing_horizontal_mode("horizontal")

        assert ent._ssot_baselines.get(TrackableSetting.SWING_HORIZONTAL_MODE) == "horizontal"

    async def test_turn_on_forwards(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            await ent.async_turn_on()

            mock_call.assert_called_once()
            args = mock_call.call_args[0]
            # domain = "climate", service = "turn_on"
            assert "climate" in args
            assert "turn_on" in args
            assert any(
                isinstance(a, dict) and a.get(ATTR_ENTITY_ID) == REAL_THERMOSTAT_ENTITY
                for a in args
            )

    async def test_turn_off_forwards(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock) as mock_call:
            await ent.async_turn_off()

            mock_call.assert_called_once()
            args = mock_call.call_args[0]
            assert "climate" in args
            assert "turn_off" in args
            assert any(
                isinstance(a, dict) and a.get(ATTR_ENTITY_ID) == REAL_THERMOSTAT_ENTITY
                for a in args
            )


# ── IT override for swing_horizontal_mode ─────────────────────────────


class TestSwingHorizontalModeITOverride:
    """When IT locks swing_horizontal_mode, the proxy shows its own baseline."""

    def test_it_returns_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity(it_settings=["swing_horizontal_mode"])
        ent._ssot_baselines[TrackableSetting.SWING_HORIZONTAL_MODE] = "horizontal"
        state = _make_full_state(swing_horizontal_mode="off")
        _seed_entity(hass, ent, state)

        assert ent.swing_horizontal_mode == "horizontal"

    def test_without_it_returns_real(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        ent._ssot_baselines[TrackableSetting.SWING_HORIZONTAL_MODE] = "horizontal"
        state = _make_full_state(swing_horizontal_mode="off")
        _seed_entity(hass, ent, state)

        assert ent.swing_horizontal_mode == "off"


# ── IT override for target_temperature_high ──────────────────────────


class TestTargetTempHighITOverride:
    """When IT locks target_temp_high, the proxy shows its own baseline."""

    def test_it_returns_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity(it_settings=["target_temp_high"])
        ent._ssot_baselines[TrackableSetting.TARGET_TEMP_HIGH] = 28.0
        state = _make_full_state(target_temp_high=25.0)
        _seed_entity(hass, ent, state)

        assert ent.target_temperature_high == 28.0

    def test_without_it_returns_real(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        ent._ssot_baselines[TrackableSetting.TARGET_TEMP_HIGH] = 28.0
        state = _make_full_state(target_temp_high=25.0)
        _seed_entity(hass, ent, state)

        assert ent.target_temperature_high == 25.0


# ── IT override for target_temperature_low ───────────────────────────


class TestTargetTempLowITOverride:
    """When IT locks target_temp_low, the proxy shows its own baseline."""

    def test_it_returns_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity(it_settings=["target_temp_low"])
        ent._ssot_baselines[TrackableSetting.TARGET_TEMP_LOW] = 16.0
        state = _make_full_state(target_temp_low=18.0)
        _seed_entity(hass, ent, state)

        assert ent.target_temperature_low == 16.0

    def test_without_it_returns_real(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        ent._ssot_baselines[TrackableSetting.TARGET_TEMP_LOW] = 16.0
        state = _make_full_state(target_temp_low=18.0)
        _seed_entity(hass, ent, state)

        assert ent.target_temperature_low == 18.0


# ── IT override for target_humidity ──────────────────────────────────


class TestTargetHumidityITOverride:
    """When IT locks target_humidity, the proxy shows its own baseline."""

    def test_it_returns_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity(it_settings=["target_humidity"])
        ent._ssot_baselines[TrackableSetting.TARGET_HUMIDITY] = 60.0
        state = _make_full_state(humidity=45)
        _seed_entity(hass, ent, state)

        assert ent.target_humidity == 60.0

    def test_without_it_returns_real(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        ent._ssot_baselines[TrackableSetting.TARGET_HUMIDITY] = 60.0
        state = _make_full_state(humidity=45)
        _seed_entity(hass, ent, state)

        assert ent.target_humidity == 45


# ── Humidity SSOT baseline update ────────────────────────────────────


class TestSetHumiditySSoT:
    """async_set_humidity updates SSOT baseline when enabled."""

    async def test_set_humidity_updates_ssot_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity(
            ssot_settings=["target_humidity"],
        )
        ent._ssot_baselines[TrackableSetting.TARGET_HUMIDITY] = 45.0
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_humidity(60)

        assert ent._ssot_baselines.get(TrackableSetting.TARGET_HUMIDITY) == 60.0

    async def test_set_humidity_no_ssot_no_baseline(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()  # No SSOT
        _seed_entity(hass, ent)

        with patch(PATCH_ASYNC_CALL, new_callable=AsyncMock):
            await ent.async_set_humidity(60)

        assert ent._ssot_baselines.get(TrackableSetting.TARGET_HUMIDITY) is None


# ── Holistic mirror test ──────────────────────────────────────────────


class TestHolisticMirroring:
    """End-to-end: a full-featured device is completely mirrored by the proxy."""

    def test_full_device_all_properties_match(
        self, hass: HomeAssistant, make_entity
    ) -> None:
        ent = make_entity()
        _seed_entity(hass, ent)

        # HVAC
        assert ent.hvac_mode == HVACMode.HEAT
        assert ent.hvac_action == HVACAction.HEATING
        assert HVACMode.OFF in ent.hvac_modes
        assert HVACMode.HEAT in ent.hvac_modes

        # Temperature
        assert ent.target_temperature == 24.0  # virtual target
        assert ent.target_temperature_high == 25.0
        assert ent.target_temperature_low == 18.0
        assert ent.target_temperature_step == 0.5
        assert ent.min_temp == 5.0
        assert ent.max_temp == 35.0

        # Humidity
        assert ent.current_humidity == 50
        assert ent.target_humidity == 45
        assert ent.min_humidity == 20
        assert ent.max_humidity == 80

        # Fan
        assert ent.fan_mode == "auto"
        assert ent.fan_modes == ["auto", "low", "medium", "high"]

        # Swing
        assert ent.swing_mode == "off"
        assert ent.swing_modes == ["off", "vertical"]
        assert ent.swing_horizontal_mode == "off"
        assert ent.swing_horizontal_modes == ["off", "horizontal"]

        # Features
        features = ent.supported_features
        assert features & ClimateEntityFeature.TARGET_TEMPERATURE
        assert features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        assert features & ClimateEntityFeature.TARGET_HUMIDITY
        assert features & ClimateEntityFeature.FAN_MODE
        assert features & ClimateEntityFeature.SWING_MODE
        assert features & ClimateEntityFeature.SWING_HORIZONTAL_MODE
        assert features & ClimateEntityFeature.TURN_ON
        assert features & ClimateEntityFeature.TURN_OFF
        assert features & ClimateEntityFeature.PRESET_MODE
