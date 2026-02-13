"""Tests for the TrackableSetting enum."""

from __future__ import annotations

import pytest

from homeassistant.core import State

from custom_components.thermostat_proxy.climate import (
    PENDING_REQUEST_TOLERANCE_MAX,
    TrackableSetting,
    _CORRECTION_GROUPS,
    _SSOT_EXPORTABLE_SETTINGS,
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


# ── state_key ────────────────────────────────────────────────────────


class TestStateKey:
    """state_key defaults to attr_key unless overridden."""

    def test_state_key_default(self) -> None:
        """For most settings, state_key equals attr_key."""
        for setting in TrackableSetting:
            if setting != TrackableSetting.TARGET_HUMIDITY:
                assert setting.state_key == setting.attr_key, (
                    f"{setting.name}: state_key should default to attr_key"
                )

    def test_state_key_override_humidity(self) -> None:
        """TARGET_HUMIDITY.state_key is 'humidity', not 'target_humidity'."""
        assert TrackableSetting.TARGET_HUMIDITY.state_key == "humidity"
        assert TrackableSetting.TARGET_HUMIDITY.attr_key == "target_humidity"

    def test_read_from_uses_state_key(self) -> None:
        """TARGET_HUMIDITY reads from 'humidity' attribute, not 'target_humidity'."""
        state = _make_state("heat", humidity=55)
        assert TrackableSetting.TARGET_HUMIDITY.read_from(state) == 55.0

    def test_read_from_humidity_none_without_key(self) -> None:
        """TARGET_HUMIDITY returns None when 'humidity' is absent."""
        state = _make_state("heat", target_humidity=55)  # wrong key
        assert TrackableSetting.TARGET_HUMIDITY.read_from(state) is None


# ── Metadata fields ──────────────────────────────────────────────────


class TestMetadataFields:
    """New metadata fields on TrackableSetting are correctly set."""

    def test_service_name_present(self) -> None:
        """Every setting has a non-empty service_name."""
        for setting in TrackableSetting:
            assert setting.service_name, f"{setting.name} missing service_name"

    def test_service_attr_present(self) -> None:
        """Every setting has a non-empty service_attr."""
        for setting in TrackableSetting:
            assert setting.service_attr, f"{setting.name} missing service_attr"

    def test_ssot_export_key_present_for_exportable(self) -> None:
        """All exportable settings have non-None ssot_export_key."""
        for setting in _SSOT_EXPORTABLE_SETTINGS:
            assert setting.ssot_export_key is not None, (
                f"{setting.name} is exportable but ssot_export_key is None"
            )

    def test_temperature_not_exportable(self) -> None:
        """TEMPERATURE uses ATTR_REAL_TARGET_TEMPERATURE, so ssot_export_key is None."""
        assert TrackableSetting.TEMPERATURE.ssot_export_key is None
        assert TrackableSetting.TEMPERATURE not in _SSOT_EXPORTABLE_SETTINGS

    def test_correction_groups(self) -> None:
        """temp_range group contains TARGET_TEMP_HIGH and TARGET_TEMP_LOW."""
        assert "temp_range" in _CORRECTION_GROUPS
        group = _CORRECTION_GROUPS["temp_range"]
        assert TrackableSetting.TARGET_TEMP_HIGH in group
        assert TrackableSetting.TARGET_TEMP_LOW in group
        assert len(group) == 2

    def test_no_unexpected_correction_groups(self) -> None:
        """Only temp_range should exist as a correction group."""
        for key in _CORRECTION_GROUPS:
            assert key == "temp_range", f"Unexpected correction group: {key}"

    def test_humidity_service_attr(self) -> None:
        """TARGET_HUMIDITY.service_attr is 'humidity', not 'target_humidity'."""
        assert TrackableSetting.TARGET_HUMIDITY.service_attr == "humidity"

    def test_temp_range_share_service_name(self) -> None:
        """TARGET_TEMP_HIGH and TARGET_TEMP_LOW both use set_temperature."""
        assert TrackableSetting.TARGET_TEMP_HIGH.service_name == "set_temperature"
        assert TrackableSetting.TARGET_TEMP_LOW.service_name == "set_temperature"
