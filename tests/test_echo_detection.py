"""Tests for deterministic echo detection (_is_echo_of_our_change)."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant, State

from custom_components.thermostat_proxy.climate import (
    TrackableSetting,
    _CORE_TRACKED_SETTINGS,
)

from .conftest import REAL_THERMOSTAT_ENTITY


def _state(
    hvac: str = "heat",
    temperature: float = 22.0,
    fan_mode: str | None = "auto",
    swing_mode: str | None = "off",
) -> State:
    """Build a physical thermostat State for echo detection tests."""
    attrs: dict = {"temperature": temperature}
    if fan_mode is not None:
        attrs["fan_mode"] = fan_mode
    if swing_mode is not None:
        attrs["swing_mode"] = swing_mode
    return State(REAL_THERMOSTAT_ENTITY, hvac, attrs)


@pytest.fixture
def entity(hass: HomeAssistant, make_entity):
    """Entity with SSOT enabled and baselines pre-seeded."""
    ent = make_entity(
        ssot_settings=["hvac_mode", "temperature", "fan_mode", "swing_mode"],
    )
    # Seed baselines so echo detection has something to compare against.
    ent._ssot_baselines[TrackableSetting.HVAC_MODE] = "heat"
    ent._last_real_target_temp = 22.0
    ent._ssot_baselines[TrackableSetting.TEMPERATURE] = 22.0
    ent._ssot_baselines[TrackableSetting.FAN_MODE] = "auto"
    ent._ssot_baselines[TrackableSetting.SWING_MODE] = "off"
    # Ensure all four settings are active.
    ent._active_tracked_settings = {
        TrackableSetting.HVAC_MODE,
        TrackableSetting.TEMPERATURE,
        TrackableSetting.FAN_MODE,
        TrackableSetting.SWING_MODE,
    }
    return ent


class TestIsEchoOfOurChange:
    """_is_echo_of_our_change returns True only when every changed attr
    matches either baseline or a pending request."""

    def test_all_match_baseline(self, entity) -> None:
        new = _state(hvac="heat", temperature=22.0, fan_mode="auto", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is True

    def test_matches_pending_not_baseline(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.HVAC_MODE, "cool")
        new = _state(hvac="cool", temperature=22.0, fan_mode="auto", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is True

    def test_not_echo_when_differs_from_both(self, entity) -> None:
        # No pending for HVAC_MODE, and state differs from baseline.
        new = _state(hvac="cool", temperature=22.0, fan_mode="auto", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is False

    def test_temperature_within_tolerance(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.5)
        new = _state(hvac="heat", temperature=22.48, fan_mode="auto", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is True

    def test_not_echo_one_attr_unexplained(self, entity) -> None:
        # HVAC matches pending, but fan changed with no pending and no baseline match.
        entity._record_setting_request(TrackableSetting.HVAC_MODE, "cool")
        new = _state(hvac="cool", temperature=22.0, fan_mode="high", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is False

    def test_skips_none_baseline(self, entity) -> None:
        # If a baseline is None, echo detection should skip that setting.
        del entity._ssot_baselines[TrackableSetting.FAN_MODE]
        new = _state(hvac="heat", temperature=22.0, fan_mode="high", swing_mode="off")
        # fan_mode baseline is None → skipped → still echo
        assert entity._is_echo_of_our_change(new) is True

    def test_partial_pendings(self, entity) -> None:
        """Some attrs match baseline, one matches a pending → echo."""
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 23.0)
        new = _state(hvac="heat", temperature=23.0, fan_mode="auto", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is True

    def test_ignores_inactive_settings(self, entity) -> None:
        """If a setting is not in _active_tracked_settings, changes to it are ignored."""
        entity._active_tracked_settings = {
            TrackableSetting.HVAC_MODE,
            TrackableSetting.TEMPERATURE,
        }
        # fan_mode changed but is NOT active → ignored
        new = _state(hvac="heat", temperature=22.0, fan_mode="high", swing_mode="off")
        assert entity._is_echo_of_our_change(new) is True

    def test_numeric_at_boundary_inside(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.5)
        # 22.5 + 0.49 = 22.99 → within default tolerance 0.5
        new = _state(
            hvac="heat", temperature=22.99, fan_mode="auto", swing_mode="off"
        )
        assert entity._is_echo_of_our_change(new) is True

    def test_numeric_at_boundary_outside(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.5)
        # 22.5 + 0.51 = 23.01 → outside default tolerance 0.5
        new = _state(
            hvac="heat", temperature=23.01, fan_mode="auto", swing_mode="off"
        )
        assert entity._is_echo_of_our_change(new) is False


class TestConsumeEchoPendingRequests:
    """_consume_echo_pending_requests removes matched pendings."""

    def test_consumes_matched_requests(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.HVAC_MODE, "cool")
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 23.0)
        new = _state(hvac="cool", temperature=23.0, fan_mode="auto", swing_mode="off")

        entity._consume_echo_pending_requests(new)

        assert not entity._has_pending_setting_request(
            TrackableSetting.HVAC_MODE, "cool"
        )
        assert not entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 23.0, tolerance=0.05
        )
