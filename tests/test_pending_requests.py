"""Tests for the pending request tracking subsystem."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.thermostat_proxy.climate import (
    MAX_TRACKED_REAL_TARGET_REQUESTS,
    PENDING_REQUEST_TIMEOUT,
    TrackableSetting,
)

from .conftest import make_thermostat_state, make_sensor_state


@pytest.fixture
def entity(hass: HomeAssistant, make_entity):
    """A basic entity for pending request tests (no SSOT)."""
    return make_entity()


class TestRecordAndHas:
    """Recording and checking pending requests."""

    def test_record_and_has_pending(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.0)
        assert entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 22.0, tolerance=0.05
        )

    def test_has_returns_false_for_unrecorded(self, entity) -> None:
        assert not entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 25.0, tolerance=0.05
        )

    def test_record_and_consume_enum_setting(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.HVAC_MODE, "cool")
        assert entity._has_pending_setting_request(TrackableSetting.HVAC_MODE, "cool")
        consumed = entity._consume_pending_setting_request(
            TrackableSetting.HVAC_MODE, "cool"
        )
        assert consumed
        assert not entity._has_pending_setting_request(
            TrackableSetting.HVAC_MODE, "cool"
        )


class TestConsume:
    """Consuming pending requests."""

    def test_consume_removes_first_match(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.0)
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 23.0)

        consumed = entity._consume_pending_setting_request(
            TrackableSetting.TEMPERATURE, 22.0, tolerance=0.05
        )
        assert consumed
        assert not entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 22.0, tolerance=0.05
        )
        # 23.0 should still be present
        assert entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 23.0, tolerance=0.05
        )

    def test_consume_returns_false_when_no_match(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.0)
        assert not entity._consume_pending_setting_request(
            TrackableSetting.TEMPERATURE, 30.0, tolerance=0.05
        )


class TestCleanupAndEviction:
    """Expiration and FIFO eviction."""

    def test_cleanup_removes_expired_requests(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.0)

        # Advance time past the timeout
        import time

        future = time.monotonic() + PENDING_REQUEST_TIMEOUT + 1.0
        with patch("time.monotonic", return_value=future):
            assert not entity._has_pending_setting_request(
                TrackableSetting.TEMPERATURE, 22.0, tolerance=0.05
            )

    def test_max_tracked_evicts_oldest(self, entity) -> None:
        # Record MAX + 1 requests
        for i in range(MAX_TRACKED_REAL_TARGET_REQUESTS + 1):
            entity._record_setting_request(
                TrackableSetting.TEMPERATURE, 20.0 + i
            )

        # The oldest (20.0) should have been evicted
        assert not entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 20.0, tolerance=0.05
        )
        # The most recent should still be present
        last_val = 20.0 + MAX_TRACKED_REAL_TARGET_REQUESTS
        assert entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, last_val, tolerance=0.05
        )


class TestToleranceMatching:
    """Tolerance-based matching for numeric settings."""

    def test_within_tolerance_matches(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.0)
        assert entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 22.04, tolerance=0.05
        )

    def test_outside_tolerance_no_match(self, entity) -> None:
        entity._record_setting_request(TrackableSetting.TEMPERATURE, 22.0)
        assert not entity._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, 22.06, tolerance=0.05
        )
