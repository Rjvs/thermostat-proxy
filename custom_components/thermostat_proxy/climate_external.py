"""External thermostat state processing for Thermostat Proxy."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

_LOGGER = logging.getLogger(__name__)


def handle_real_state_event(entity, event) -> bool:
    """Handle a real thermostat state event.

    Returns True when the caller should continue normal post-processing
    (schedule realignment + write state). Returns False when processing
    is complete (typically due to rejection/correction).
    """

    previous_real_state = entity._real_state
    new_state = event.data.get("new_state")
    entity._real_state = new_state
    entity._update_real_temperature_limits()
    if not new_state:
        entity.async_write_ha_state()
        return False

    entity._temperature_unit = entity._discover_temperature_unit()

    # Availability transitions do not modify canonical state.
    if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return True
    entity._remember_non_off_hvac_mode(new_state.state)

    if entity._single_source_of_truth and not entity._ssot_baselines:
        entity._seed_ssot_baselines(new_state)
        _LOGGER.info(
            "Single source of truth: initialized baseline for %s (mode=%s)",
            entity._real_entity_id,
            new_state.state,
        )

    changes = _collect_canonical_changes(entity, new_state, previous_real_state)

    if not changes:
        return True

    if _consume_echo_changes(entity, changes):
        _LOGGER.debug("State update reconciled as deterministic echo")
        return True

    if _should_reject_external_change(entity, changes):
        change_str = "; ".join(
            f"{setting.attr_key}: {canonical!r} -> {incoming!r}"
            for setting, canonical, incoming in changes
        )
        _LOGGER.warning(
            "Rejected external change from %s: %s",
            entity._real_entity_id,
            change_str,
        )
        entity._real_state = previous_real_state
        entity.hass.async_create_task(entity._async_correct_physical_device())
        entity.async_write_ha_state()
        return False

    _accept_external_change(entity, changes)
    return True


def _collect_canonical_changes(entity, new_state, previous_real_state) -> list[tuple[Any, Any, Any]]:
    """Return list of (setting, canonical_value, incoming_value) changes."""

    changes: list[tuple[Any, Any, Any]] = []
    for setting in entity._active_tracked_settings:
        incoming = setting.read_from(new_state)
        if incoming is None:
            continue

        canonical = entity._get_ssot_baseline(setting)
        if canonical is None and previous_real_state is not None:
            canonical = setting.read_from(previous_real_state)
        if canonical is None:
            continue

        if setting.values_match(incoming, canonical):
            continue

        changes.append((setting, canonical, incoming))
    return changes


def _should_reject_external_change(entity, changes: list[tuple[Any, Any, Any]]) -> bool:
    """Return True when policy requires rejecting this external change."""

    # Ignore Thermostat mode rejects all external tracked-setting changes.
    if entity._ignore_thermostat:
        return True

    # Single Source of Truth accepts at most one externally changed SSOT setting.
    if entity._single_source_of_truth:
        ssot_changes = [
            setting
            for setting, _canonical, _incoming in changes
            if setting in entity._ssot_settings
        ]
        if len(ssot_changes) > 1:
            return True

    return False


def _consume_echo_changes(entity, changes: list[tuple[Any, Any, Any]]) -> bool:
    """Consume pending requests if *changes* are fully explained as echoes."""

    match_index: dict[Any, int] = {}

    for setting, _canonical, incoming in changes:
        idx = _find_pending_match_index(entity, setting, incoming)
        if idx is None:
            return False
        match_index[setting] = idx

    for setting, idx in match_index.items():
        requests = entity._pending_setting_requests[setting]
        del requests[: idx + 1]
        incoming = next(
            val for s, _canonical, val in changes if s == setting
        )
        entity._set_ssot_baseline(setting, incoming)

    return True


def _find_pending_match_index(entity, setting, incoming) -> int | None:
    """Find the latest pending request index matching *incoming*."""

    entity._cleanup_pending_requests(setting)
    requests = entity._pending_setting_requests[setting]
    tolerance = entity._pending_request_tolerance() if setting.is_numeric else None
    found: int | None = None
    for idx, (pending, _ts) in enumerate(requests):
        if setting.values_match(incoming, pending, tolerance):
            found = idx
    return found


def _accept_external_change(entity, changes: list[tuple[Any, Any, Any]]) -> None:
    """Apply accepted external changes to canonical state."""

    changed_settings = {setting for setting, _canonical, _incoming in changes}

    # Update SSOT baselines only for SSOT-tracked settings.
    for setting, _canonical, incoming in changes:
        if setting in entity._ssot_settings:
            entity._set_ssot_baseline(setting, incoming)

    real_target = entity._get_real_target_temperature()
    if real_target is not None:
        entity._last_real_target_temp = real_target

    temperature_setting = next(
        (setting for setting in changed_settings if setting.attr_key == "temperature"),
        None,
    )
    if temperature_setting is not None and real_target is not None:
        entity._handle_external_real_target_change(real_target)
