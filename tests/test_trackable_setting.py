"""Tests for the TrackableSetting enum."""

from __future__ import annotations

import pytest

from homeassistant.core import State

from custom_components.thermostat_proxy.climate import (
    PENDING_REQUEST_TOLERANCE_MAX,
    TrackableSetting,
    _TRACKABLE_SETTING_BY_KEY,
)


def _make_state(
    state_str: str = "heat",
    **attrs,
) -> State:
    """Shorthand to build a State object."""
    return State("climate.test", state_str, attrs)


# ── read_from ─────────────────────────────────────────────────────────


class TestReadFrom:
    """TrackableSetting.read_from extracts the correct value."""

    def test_hvac_mode_reads_from_state_string(self) -> None:
        state = _make_state("heat", temperature=22.0)
        assert TrackableSetting.HVAC_MODE.read_from(state) == "heat"

    def test_temperature_reads_from_attributes(self) -> None:
        state = _make_state("heat", temperature=22.5)
        assert TrackableSetting.TEMPERATURE.read_from(state) == 22.5

    def test_read_from_returns_none_for_missing_attribute(self) -> None:
        state = _make_state("heat")  # no fan_mode attribute
        assert TrackableSetting.FAN_MODE.read_from(state) is None

    def test_numeric_coerces_string_to_float(self) -> None:
        """read_from should coerce string temperatures via _coerce_temperature."""
        state = _make_state("heat", temperature="22.5")
        assert TrackableSetting.TEMPERATURE.read_from(state) == 22.5

    def test_numeric_coerces_none(self) -> None:
        """Numeric read_from returns None for None attribute."""
        state = _make_state("heat", temperature=None)
        assert TrackableSetting.TEMPERATURE.read_from(state) is None


# ── values_match ──────────────────────────────────────────────────────


class TestValuesMatch:
    """TrackableSetting.values_match compares correctly per type."""

    def test_numeric_within_tolerance(self) -> None:
        assert TrackableSetting.TEMPERATURE.values_match(22.0, 22.04, tolerance=0.05)

    def test_numeric_outside_tolerance(self) -> None:
        assert not TrackableSetting.TEMPERATURE.values_match(22.0, 22.1, tolerance=0.05)

    def test_numeric_uses_default_tolerance(self) -> None:
        # Default tolerance = PENDING_REQUEST_TOLERANCE_MAX = 0.5
        assert TrackableSetting.TEMPERATURE.values_match(22.0, 22.49)
        assert not TrackableSetting.TEMPERATURE.values_match(22.0, 22.6)

    def test_enum_exact_match(self) -> None:
        assert TrackableSetting.FAN_MODE.values_match("auto", "auto")

    def test_enum_mismatch(self) -> None:
        assert not TrackableSetting.FAN_MODE.values_match("auto", "low")

    def test_none_vs_value(self) -> None:
        assert not TrackableSetting.TEMPERATURE.values_match(None, 22.0)

    def test_none_vs_none(self) -> None:
        assert TrackableSetting.TEMPERATURE.values_match(None, None)

    def test_value_vs_none(self) -> None:
        assert not TrackableSetting.FAN_MODE.values_match("auto", None)


# ── _TRACKABLE_SETTING_BY_KEY ─────────────────────────────────────────


class TestLookupByKey:
    """The attr_key lookup dict maps correctly."""

    def test_all_settings_present(self) -> None:
        for setting in TrackableSetting:
            assert setting.attr_key in _TRACKABLE_SETTING_BY_KEY
            assert _TRACKABLE_SETTING_BY_KEY[setting.attr_key] is setting

    def test_lookup_hvac_mode(self) -> None:
        assert _TRACKABLE_SETTING_BY_KEY["hvac_mode"] is TrackableSetting.HVAC_MODE

    def test_lookup_temperature(self) -> None:
        assert _TRACKABLE_SETTING_BY_KEY["temperature"] is TrackableSetting.TEMPERATURE
