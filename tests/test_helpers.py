"""Tests for module-level helper functions in climate.py."""

from __future__ import annotations

import math

import pytest

from custom_components.thermostat_proxy.climate import (
    _coerce_temperature,
    _coerce_positive_float,
)


# ── _coerce_temperature ───────────────────────────────────────────────


class TestCoerceTemperature:
    """Tests for _coerce_temperature."""

    def test_valid_string(self) -> None:
        assert _coerce_temperature("22.5") == 22.5

    def test_valid_int(self) -> None:
        assert _coerce_temperature(22) == 22.0

    def test_valid_float(self) -> None:
        assert _coerce_temperature(22.5) == 22.5

    def test_none_returns_none(self) -> None:
        assert _coerce_temperature(None) is None

    def test_non_numeric_string_returns_none(self) -> None:
        assert _coerce_temperature("abc") is None

    def test_nan_returns_none(self) -> None:
        assert _coerce_temperature(float("nan")) is None

    def test_zero(self) -> None:
        assert _coerce_temperature(0) == 0.0

    def test_negative(self) -> None:
        assert _coerce_temperature(-5.0) == -5.0


# ── _coerce_positive_float ────────────────────────────────────────────


class TestCoercePositiveFloat:
    """Tests for _coerce_positive_float."""

    def test_positive_value(self) -> None:
        assert _coerce_positive_float(0.5) == 0.5

    def test_zero_returns_none(self) -> None:
        assert _coerce_positive_float(0) is None

    def test_negative_returns_none(self) -> None:
        assert _coerce_positive_float(-1) is None

    def test_none_returns_none(self) -> None:
        assert _coerce_positive_float(None) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _coerce_positive_float("abc") is None
