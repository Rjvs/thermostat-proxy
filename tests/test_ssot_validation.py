"""Tests for the full SSOT/IT event handler flow.

These tests exercise _async_handle_real_state_event Section A — the SSOT
validation / echo detection / IT blocking path — by constructing entities
via make_entity and directly calling the internal methods.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant, State

from custom_components.thermostat_proxy.climate import (
    TrackableSetting,
)

from .conftest import REAL_THERMOSTAT_ENTITY


def _state(
    hvac: str = "heat",
    temperature: float = 22.0,
    fan_mode: str | None = "auto",
    swing_mode: str | None = "off",
    **extra,
) -> State:
    attrs: dict = {"temperature": temperature, **extra}
    if fan_mode is not None:
        attrs["fan_mode"] = fan_mode
    if swing_mode is not None:
        attrs["swing_mode"] = swing_mode
    return State(REAL_THERMOSTAT_ENTITY, hvac, attrs)


def _seed_baselines(entity, hvac="heat", temp=22.0, fan="auto", swing="off"):
    """Seed SSOT baselines using the generic dict."""
    entity._ssot_baselines[TrackableSetting.HVAC_MODE] = hvac
    entity._last_real_target_temp = temp
    entity._ssot_baselines[TrackableSetting.TEMPERATURE] = temp
    entity._ssot_baselines[TrackableSetting.FAN_MODE] = fan
    entity._ssot_baselines[TrackableSetting.SWING_MODE] = swing


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
        # Baselines start empty
        assert TrackableSetting.HVAC_MODE not in entity._ssot_baselines

        # Use the generic seeding method
        new = _state(hvac="cool", temperature=23.0, fan_mode="high", swing_mode="on")
        entity._seed_ssot_baselines(new)

        assert entity._ssot_baselines[TrackableSetting.HVAC_MODE] == "cool"
        assert entity._ssot_baselines[TrackableSetting.FAN_MODE] == "high"
        assert entity._ssot_baselines[TrackableSetting.SWING_MODE] == "on"
        assert entity._ssot_baselines[TrackableSetting.TEMPERATURE] == 23.0


# ── SSOT change validation ────────────────────────────────────────────


class TestSSOTChangeValidation:
    """_validate_thermostat_change accepts/rejects changes correctly."""

    @pytest.fixture
    def entity(self, hass, make_entity):
        ent = make_entity(
            ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
        )
        _seed_baselines(ent)
        ent._target_temp_step = 0.5
        ent._active_tracked_settings = {
            TrackableSetting.HVAC_MODE,
            TrackableSetting.TEMPERATURE,
            TrackableSetting.FAN_MODE,
            TrackableSetting.SWING_MODE,
        }
        return ent

    def test_accepts_single_temp_step(self, entity) -> None:
        """A single-step temperature change should be accepted."""
        new = _state(hvac="heat", temperature=22.5, fan_mode="auto", swing_mode="off")
        assert entity._validate_thermostat_change(new) is True

    def test_rejects_multi_step_temp_change(self, entity) -> None:
        """A temperature jump of 3 steps should be rejected."""
        new = _state(hvac="heat", temperature=23.5, fan_mode="auto", swing_mode="off")
        assert entity._validate_thermostat_change(new) is False

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
        _seed_baselines(ent)
        ent._active_tracked_settings = {
            TrackableSetting.HVAC_MODE,
            TrackableSetting.TEMPERATURE,
            TrackableSetting.FAN_MODE,
            TrackableSetting.SWING_MODE,
        }
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
        _seed_baselines(entity)
        entity._active_tracked_settings = {
            TrackableSetting.HVAC_MODE,
            TrackableSetting.TEMPERATURE,
            TrackableSetting.FAN_MODE,
            TrackableSetting.SWING_MODE,
        }

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
