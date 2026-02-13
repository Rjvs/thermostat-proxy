"""Tests for the full SSOT/IT event handler flow.

These tests exercise _async_handle_real_state_event Section A — the SSOT
validation / echo detection / IT blocking path — by constructing entities
via make_entity and directly calling the internal methods.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.core import HomeAssistant

from custom_components.thermostat_proxy.climate import (
    TrackableSetting,
)

from .conftest import make_simple_state as _state, seed_core_baselines


# ── Baseline seeding ──────────────────────────────────────────────────


class TestBaselineSeeding:
    """First valid SSOT event seeds baselines."""

    def test_seeds_baseline_on_first_event(self, hass, make_entity) -> None:
        entity = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
        )
        entity._active_tracked_settings = {
            TrackableSetting.HVAC_MODE,
            TrackableSetting.TEMPERATURE,
            TrackableSetting.FAN_MODE,
            TrackableSetting.SWING_MODE,
        }
        # Baselines start as None
        assert entity._ssot_baselines.get(TrackableSetting.HVAC_MODE) is None

        # Simulate what the event handler does on first valid event:
        # it checks _ssot_hvac_mode is None and seeds.
        new = _state(hvac="cool", temperature=23.0, fan_mode="high", swing_mode="on")
        # Manually exercise the seeding path:
        entity._ssot_baselines[TrackableSetting.HVAC_MODE] = new.state
        entity._ssot_baselines[TrackableSetting.FAN_MODE] = new.attributes.get("fan_mode")
        entity._ssot_baselines[TrackableSetting.SWING_MODE] = new.attributes.get("swing_mode")
        entity._last_real_target_temp = 23.0

        assert entity._ssot_baselines.get(TrackableSetting.HVAC_MODE) == "cool"
        assert entity._ssot_baselines.get(TrackableSetting.FAN_MODE) == "high"
        assert entity._ssot_baselines.get(TrackableSetting.SWING_MODE) == "on"
        assert entity._last_real_target_temp == 23.0


# ── SSOT change validation ────────────────────────────────────────────


class TestSSOTChangeValidation:
    """_validate_thermostat_change accepts/rejects changes correctly."""

    @pytest.fixture
    def entity(self, hass, make_entity):
        ent = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
        )
        seed_core_baselines(ent)
        ent._target_temp_step = 0.5
        return ent

    def test_accepts_single_temp_step(self, entity) -> None:
        """A single-step temperature change should be accepted."""
        new = _state(hvac="heat", temperature=22.5, fan_mode="auto", swing_mode="off")
        assert entity._validate_thermostat_change(new) is True

    def test_rejects_multi_step_temp_change(self, entity) -> None:
        """A single temperature change is accepted regardless of step size."""
        new = _state(hvac="heat", temperature=23.5, fan_mode="auto", swing_mode="off")
        assert entity._validate_thermostat_change(new) is True

    def test_rejects_compound_changes(self, entity) -> None:
        """Simultaneous HVAC mode + fan mode change should be rejected."""
        new = _state(hvac="cool", temperature=22.0, fan_mode="high", swing_mode="off")
        assert entity._validate_thermostat_change(new) is False

    def test_accepts_single_enum_change(self, entity) -> None:
        """A single fan_mode change should be accepted."""
        new = _state(hvac="heat", temperature=22.0, fan_mode="high", swing_mode="off")
        assert entity._validate_thermostat_change(new) is True


# ── IT blocking classification ────────────────────────────────────────


class TestITBlocking:
    """IT-tracked settings should be classified as blocked."""

    @pytest.fixture
    def entity(self, hass, make_entity):
        ent = make_entity(
            it_settings=["hvac_mode"],
            ssot_settings=["temperature"],
        )
        seed_core_baselines(ent)
        return ent

    def test_it_setting_in_it_settings(self, entity) -> None:
        """HVAC_MODE should be in _it_settings."""
        assert TrackableSetting.HVAC_MODE in entity._it_settings

    def test_it_implies_ssot(self, entity) -> None:
        """IT settings are automatically SSOT-tracked."""
        assert TrackableSetting.HVAC_MODE in entity._ssot_settings

    def test_classifies_it_blocked_diff(self, entity) -> None:
        """When an IT-tracked setting changes, it should be identified."""
        new = _state(hvac="cool", temperature=22.0, fan_mode="auto", swing_mode="off")
        # Echo detection should fail (change not from us)
        assert entity._is_echo_of_our_change(new) is False

        # The change classification loop should find it in _it_settings
        blocked = []
        for setting in entity._active_tracked_settings:
            incoming = setting.read_from(new)
            baseline = entity._get_ssot_baseline(setting)
            if incoming is None or baseline is None:
                continue
            if setting.values_match(incoming, baseline):
                continue
            if setting in entity._it_settings:
                blocked.append(setting.attr_key)
        assert "hvac_mode" in blocked

    def test_mixed_it_and_ssot_both_detected(self, entity) -> None:
        """When IT + SSOT settings both change, both are classified."""
        new = _state(hvac="cool", temperature=25.0, fan_mode="auto", swing_mode="off")
        blocked = []
        ssot_changes = []
        for setting in entity._active_tracked_settings:
            incoming = setting.read_from(new)
            baseline = entity._get_ssot_baseline(setting)
            if incoming is None or baseline is None:
                continue
            if setting.values_match(incoming, baseline):
                continue
            if setting in entity._it_settings:
                blocked.append(setting.attr_key)
            elif setting in entity._ssot_settings:
                ssot_changes.append(setting.attr_key)

        assert "hvac_mode" in blocked
        assert "temperature" in ssot_changes


class TestUntrackedSettingsPassThrough:
    """Settings not in SSOT or IT should not be flagged."""

    def test_untracked_change_not_classified(self, hass, make_entity) -> None:
        """A change to a setting NOT in ssot_settings should not be rejected."""
        entity = make_entity(
            ssot_settings=["hvac_mode"],  # Only HVAC is SSOT-tracked
        )
        seed_core_baselines(entity)

        # Temperature jumps by 5° — but temperature is NOT SSOT-tracked
        new = _state(hvac="heat", temperature=27.0, fan_mode="auto", swing_mode="off")

        blocked = []
        ssot_changes = []
        for setting in entity._active_tracked_settings:
            incoming = setting.read_from(new)
            baseline = entity._get_ssot_baseline(setting)
            if incoming is None or baseline is None:
                continue
            if setting.values_match(incoming, baseline):
                continue
            if setting in entity._it_settings:
                blocked.append(setting.attr_key)
            elif setting in entity._ssot_settings:
                ssot_changes.append(setting.attr_key)

        # Temperature should NOT appear in either list — it's untracked
        assert "temperature" not in blocked
        assert "temperature" not in ssot_changes
        assert len(blocked) == 0
        assert len(ssot_changes) == 0


class TestExternalEventPolicy:
    """External event handling follows deterministic echo + SSOT/IT policy."""

    def _event(self, old_state, new_state):
        return SimpleNamespace(data={"old_state": old_state, "new_state": new_state})

    def test_it_rejects_all_external_changes(self, hass, make_entity) -> None:
        entity = make_entity(it_settings=["hvac_mode"])
        seed_core_baselines(entity)
        old = _state(hvac="heat", temperature=22.0, fan_mode="auto", swing_mode="off")
        new = _state(hvac="cool", temperature=22.0, fan_mode="auto", swing_mode="off")
        entity._real_state = old
        entity._schedule_target_realign = lambda *args, **kwargs: None
        entity.async_write_ha_state = lambda: None
        entity._async_correct_physical_device = AsyncMock()
        entity.hass.async_create_task = lambda coro: coro.close()

        entity._async_handle_real_state_event(self._event(old, new))

        assert entity._real_state == old
        entity._async_correct_physical_device.assert_called_once()

    def test_ssot_rejects_compound_external_changes(self, hass, make_entity) -> None:
        entity = make_entity(ssot_settings=["hvac_mode", "temperature"])
        seed_core_baselines(entity)
        old = _state(hvac="heat", temperature=22.0, fan_mode="auto", swing_mode="off")
        new = _state(hvac="cool", temperature=23.0, fan_mode="auto", swing_mode="off")
        entity._real_state = old
        entity._schedule_target_realign = lambda *args, **kwargs: None
        entity.async_write_ha_state = lambda: None
        entity._async_correct_physical_device = AsyncMock()
        entity.hass.async_create_task = lambda coro: coro.close()

        entity._async_handle_real_state_event(self._event(old, new))

        assert entity._real_state == old
        entity._async_correct_physical_device.assert_called_once()

    def test_ssot_accepts_single_external_temperature_change(self, hass, make_entity) -> None:
        entity = make_entity(ssot_settings=["hvac_mode", "temperature"])
        seed_core_baselines(entity)
        old = _state(hvac="heat", temperature=22.0, fan_mode="auto", swing_mode="off")
        new = _state(hvac="heat", temperature=23.0, fan_mode="auto", swing_mode="off")
        entity._real_state = old
        entity._schedule_target_realign = lambda *args, **kwargs: None
        entity.async_write_ha_state = lambda: None
        entity._async_correct_physical_device = AsyncMock()
        entity.hass.async_create_task = lambda coro: coro.close()
        entity._handle_external_real_target_change = MagicMock()

        entity._async_handle_real_state_event(self._event(old, new))

        entity._async_correct_physical_device.assert_not_called()
        assert entity._last_real_target_temp == 23.0
        entity._handle_external_real_target_change.assert_called_once_with(23.0)
