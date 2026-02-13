"""Thermostat Proxy climate entity."""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
import time
from collections.abc import Callable
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    HVACAction,
    HVACMode,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
)
from homeassistant.components.logbook import DOMAIN as LOGBOOK_DOMAIN

try:
    from homeassistant.components.logbook import SERVICE_LOG as LOGBOOK_SERVICE_LOG
except ImportError:  # Older HA versions don't expose SERVICE_LOG
    LOGBOOK_SERVICE_LOG = "log"

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .climate_external import handle_real_state_event
from .climate_model import (
    DEFAULT_PRECISION,
    MAX_TRACKED_REAL_TARGET_REQUESTS,
    PENDING_REQUEST_TIMEOUT,
    PENDING_REQUEST_TOLERANCE_MAX,
    PENDING_REQUEST_TOLERANCE_MIN,
    SensorConfig,
    TrackableSetting,
    _CORE_TRACKED_SETTINGS,
    _CORRECTION_GROUPS,
    _FEATURE_TO_SETTINGS,
    _PASSTHROUGH_FEATURES,
    _RESERVED_REAL_ATTRIBUTES,
    _SSOT_EXPORTABLE_SETTINGS,
    _TRACKABLE_SETTING_BY_KEY,
    _coerce_positive_float,
    _coerce_temperature,
)
from .const import (
    ATTR_ACTIVE_SENSOR,
    ATTR_ACTIVE_SENSOR_ENTITY_ID,
    ATTR_IGNORE_THERMOSTAT,
    ATTR_REAL_CURRENT_HUMIDITY,
    ATTR_REAL_CURRENT_TEMPERATURE,
    ATTR_REAL_TARGET_TEMPERATURE,
    ATTR_SELECTED_SENSOR_OPTIONS,
    ATTR_UNAVAILABLE_ENTITIES,
    CONF_SENSOR_ENTITY_ID,
    CONF_SENSOR_NAME,
    OVERDRIVE_ADJUSTMENT_COOL,
    OVERDRIVE_ADJUSTMENT_HEAT,
    PHYSICAL_SENSOR_NAME,
    PHYSICAL_SENSOR_SENTINEL,
)

_LOGGER = logging.getLogger(__name__)

class CustomThermostatEntity(RestoreEntity, ClimateEntity):
    """Thermostat proxy that can borrow any temperature sensor."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        real_thermostat: str,
        sensors: list[dict[str, Any]],
        default_sensor: str | None,
        unique_id: str | None,
        physical_sensor_name: str | None,
        use_last_active_sensor: bool,
        cooldown_period: float | int | datetime.timedelta = 0,
        user_min_temp: float | None = None,
        user_max_temp: float | None = None,
        single_source_of_truth: bool = False,
        ignore_thermostat: bool = False,
        ssot_settings: list[str] | None = None,
        it_settings: list[str] | None = None,
    ) -> None:
        self.hass = hass
        if isinstance(cooldown_period, (int, float)):
            self._cooldown_period = float(cooldown_period)
        else:
            self._cooldown_period = cooldown_period.total_seconds()
        self._last_real_write_time = 0.0
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._real_entity_id = real_thermostat
        self._physical_sensor_name = (
            physical_sensor_name or PHYSICAL_SENSOR_NAME
        )
        base_sensors: list[SensorConfig] = [
            SensorConfig(name=item[CONF_SENSOR_NAME], entity_id=item[CONF_SENSOR_ENTITY_ID])
            for item in sensors
        ]
        self._sensors = self._add_physical_sensor(base_sensors)
        self._sensor_lookup: dict[str, SensorConfig] = {
            sensor.name: sensor for sensor in self._sensors
        }
        self._configured_default_sensor = (
            default_sensor if default_sensor in self._sensor_lookup else None
        )
        self._use_last_active_sensor = use_last_active_sensor
        if self._configured_default_sensor:
            self._selected_sensor_name = self._configured_default_sensor
        else:
            self._selected_sensor_name = self._sensors[0].name
        self._sensor_states: dict[str, State | None] = {}
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
        )
        self._virtual_target_temperature: float | None = None
        self._temperature_unit: str | None = None
        self._real_state: State | None = None
        self._pending_setting_requests: dict[
            TrackableSetting, list[tuple[Any, float]]
        ] = {s: [] for s in TrackableSetting}
        self._last_real_target_temp: float | None = None
        self._unsub_listeners: list[Callable[[], None]] = []
        self._min_temp: float | None = None
        self._max_temp: float | None = None
        self._user_min_temp: float | None = user_min_temp
        self._user_max_temp: float | None = user_max_temp
        self._target_temp_step: float | None = None
        self._precision_override: float | None = None
        self._entity_health: dict[str, bool] = {}
        self._command_lock = asyncio.Lock()
        self._sensor_realign_task: asyncio.Task | None = None
        self._suppress_sync_logs_until: float | None = None
        self._cooldown_timer_unsub: Callable[[], None] | None = None
        self._last_non_off_hvac_mode: HVACMode | None = None
        self._startup_complete = False
        # Per-setting SSOT / Ignore-Thermostat configuration.
        # Migrate old boolean config to new per-setting lists.
        if ssot_settings is not None:
            self._ssot_settings: set[TrackableSetting] = {
                _TRACKABLE_SETTING_BY_KEY[s]
                for s in ssot_settings
                if s in _TRACKABLE_SETTING_BY_KEY
            }
        elif single_source_of_truth:
            # Old boolean → all settings
            self._ssot_settings = set(TrackableSetting)
        else:
            self._ssot_settings = set()

        if it_settings is not None:
            self._it_settings: set[TrackableSetting] = {
                _TRACKABLE_SETTING_BY_KEY[s]
                for s in it_settings
                if s in _TRACKABLE_SETTING_BY_KEY
            }
        elif ignore_thermostat:
            # Old boolean → all settings
            self._it_settings = set(TrackableSetting)
        else:
            self._it_settings = set()

        # IT implies SSOT for those settings.
        self._ssot_settings |= self._it_settings

        self._ssot_baselines: dict[TrackableSetting, Any] = {}
        # Active tracked settings — populated in _update_real_temperature_limits
        # based on device capabilities.  Start with core set.
        self._active_tracked_settings: set[TrackableSetting] = set(
            _CORE_TRACKED_SETTINGS
        )

    async def async_added_to_hass(self) -> None:
        """Finish setup when entity is added."""

        await super().async_added_to_hass()
        await self._async_restore_state()
        self._real_state = self.hass.states.get(self._real_entity_id)
        self._update_real_temperature_limits()
        for sensor in self._sensors:
            if sensor.is_physical:
                continue
            self._sensor_states[sensor.entity_id] = self.hass.states.get(sensor.entity_id)
            self._update_sensor_health_from_state(
                sensor.entity_id, self._sensor_states[sensor.entity_id]
            )
        self._temperature_unit = self._discover_temperature_unit()
        self._remember_non_off_hvac_mode(self._real_state.state if self._real_state else None)
        if self._virtual_target_temperature is None:
            self._virtual_target_temperature = self._apply_target_constraints(
                self._get_real_target_temperature()
                or self._get_active_sensor_temperature()
                or self._get_real_current_temperature()
            )
        if self._single_source_of_truth:
            # Only seed from the physical device when we have no restored
            # values AND the device is in a valid (non-unavailable) state.
            if (
                not self._ssot_baselines
                and self._real_state
                and self._real_state.state
                not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            ):
                self._seed_ssot_baselines(self._real_state)
        await self._async_subscribe_to_states()
        self._startup_complete = True

    async def _async_subscribe_to_states(self) -> None:
        """Listen for updates to real thermostat and sensors."""

        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                [self._real_entity_id],
                self._async_handle_real_state_event,
            )
        )

        sensor_entity_ids = [
            sensor.entity_id
            for sensor in self._sensors
            if not sensor.is_physical and sensor.entity_id
        ]
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                sensor_entity_ids,
                self._async_handle_sensor_state_event,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners when entity is removed."""

        await super().async_will_remove_from_hass()
        if self._sensor_realign_task and not self._sensor_realign_task.done():
            self._sensor_realign_task.cancel()
        if self._cooldown_timer_unsub:
            self._cooldown_timer_unsub()
            self._cooldown_timer_unsub = None
        while self._unsub_listeners:
            unsubscribe = self._unsub_listeners.pop()
            unsubscribe()

    @callback
    def _async_handle_real_state_event(self, event) -> None:
        """Handle updates to the linked thermostat."""
        if not handle_real_state_event(self, event):
            return
        self._schedule_target_realign()
        self.async_write_ha_state()

    @callback
    def _async_handle_sensor_state_event(self, event) -> None:
        """Handle updates to any configured sensor."""

        entity_id = event.data.get("entity_id")
        new_state: State | None = event.data.get("new_state")
        if entity_id:
            self._sensor_states[entity_id] = new_state
        self._update_sensor_health_from_state(entity_id, new_state)
        if self._is_active_sensor_entity(entity_id):
            self._schedule_target_realign()
        self.async_write_ha_state()

    def _is_active_sensor_entity(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        sensor = self._sensor_lookup.get(self._selected_sensor_name)
        if not sensor or sensor.is_physical:
            return False
        return sensor.entity_id == entity_id

    def _schedule_target_realign(self, retry: bool = False) -> None:
        if self._sensor_realign_task and not self._sensor_realign_task.done():
            return

        async def _run():
            try:
                await self._async_realign_real_target_from_sensor(retry=retry)
            finally:
                self._sensor_realign_task = None

        self._sensor_realign_task = self.hass.async_create_task(_run())

    def _handle_external_real_target_change(self, real_target: float) -> None:
        """React to target changes made outside the proxy."""

        self._virtual_target_temperature = self._apply_target_constraints(real_target)

        switched = self._selected_sensor_name != self._physical_sensor_name
        self._selected_sensor_name = self._physical_sensor_name
        self.async_write_ha_state()

        self.hass.async_create_task(
            self._async_log_physical_override(real_target, switched)
        )

    def _validate_thermostat_change(self, new_state: State) -> bool:
        """Return True when at most one tracked setting changed from canonical state."""
        changes: list[str] = []

        for setting in self._active_tracked_settings:
            known_good = self._get_ssot_baseline(setting)
            if known_good is None:
                continue

            new_val = setting.read_from(new_state)
            if new_val is None:
                continue

            if setting.is_numeric:
                diff = abs(new_val - known_good)
                if diff > 0.01:
                    changes.append(f"{setting.attr_key} {known_good} -> {new_val}")
            else:
                if new_val != known_good:
                    changes.append(
                        f"{setting.attr_key} {known_good!r} -> {new_val!r}"
                    )

        if len(changes) > 1:
            _LOGGER.debug(
                "Single source of truth: %d simultaneous changes detected: %s",
                len(changes),
                "; ".join(changes),
            )
            return False

        return True

    async def _async_correct_physical_device(self) -> None:
        """Reset the physical device back to the proxy's known-good state.

        Reads the *live* device state from Home Assistant rather than the
        cached ``_real_state`` because (a) we deliberately restore
        ``_real_state`` to the pre-rejection value so that properties like
        ``hvac_mode`` expose the known-good value, and (b) this coroutine
        runs asynchronously so the device may have changed again by now.
        """
        device_state = self.hass.states.get(self._real_entity_id)
        if not device_state or device_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        corrections: list[str] = []
        corrected_groups: set[str] = set()

        for setting in self._active_tracked_settings:
            baseline = self._get_ssot_baseline(setting)
            if baseline is None:
                continue

            # Skip if this setting's group was already corrected.
            if setting.correction_group and setting.correction_group in corrected_groups:
                continue

            current = setting.read_from(device_state)
            if current is None:
                continue
            if setting.values_match(current, baseline):
                continue

            # Build service call payload.
            if setting.correction_group:
                # Grouped correction (e.g. temp_range): send ALL settings in
                # the group in a single service call.
                corrected_groups.add(setting.correction_group)
                group_settings = _CORRECTION_GROUPS[setting.correction_group]
                payload: dict[str, Any] = {ATTR_ENTITY_ID: self._real_entity_id}
                for gs in group_settings:
                    gs_baseline = self._get_ssot_baseline(gs)
                    if gs_baseline is not None:
                        payload[gs.service_attr] = gs_baseline
                        self._record_setting_request(gs, gs_baseline)
                        corrections.append(f"{gs.attr_key}={gs_baseline}")
                service = setting.service_name
            else:
                # Individual correction.
                payload = {
                    ATTR_ENTITY_ID: self._real_entity_id,
                    setting.service_attr: baseline,
                }
                service = setting.service_name
                self._record_setting_request(setting, baseline)
                corrections.append(f"{setting.attr_key}={baseline}")

            self._last_real_write_time = time.monotonic()
            try:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    service,
                    payload,
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.error(
                    "Single source of truth: failed to correct %s on %s: %s",
                    setting.attr_key,
                    self._real_entity_id,
                    err,
                )

        if corrections:
            await self.hass.services.async_call(
                LOGBOOK_DOMAIN,
                LOGBOOK_SERVICE_LOG,
                {
                    "name": self.name,
                    "entity_id": self.entity_id,
                    "message": (
                        "Single source of truth: corrected %s after "
                        "rejecting invalid input: %s"
                        % (
                            self._real_entity_id,
                            ", ".join(corrections),
                        )
                    ),
                },
                blocking=False,
            )

    # ---- Generic pending-request tracking for all TrackableSettings ----

    def _record_setting_request(
        self, setting: TrackableSetting, value: Any
    ) -> None:
        """Record that we sent *value* for *setting* to the physical device."""
        requests = self._pending_setting_requests[setting]
        requests.append((value, time.monotonic()))
        if len(requests) > MAX_TRACKED_REAL_TARGET_REQUESTS:
            requests.pop(0)

    def _has_pending_setting_request(
        self,
        setting: TrackableSetting,
        value: Any,
        tolerance: float | None = None,
    ) -> bool:
        """Return True if we have a pending request matching *value*."""
        self._cleanup_pending_requests(setting)
        for pending, _ts in self._pending_setting_requests[setting]:
            if setting.values_match(value, pending, tolerance):
                return True
        return False

    def _consume_pending_setting_request(
        self,
        setting: TrackableSetting,
        value: Any,
        tolerance: float | None = None,
    ) -> bool:
        """Consume (remove) the first pending request matching *value*.

        Returns True if a match was found and consumed.
        """
        self._cleanup_pending_requests(setting)
        requests = self._pending_setting_requests[setting]
        for i, (pending, _ts) in enumerate(requests):
            if setting.values_match(value, pending, tolerance):
                del requests[i]
                return True
        return False

    def _remove_pending_setting_request(
        self, setting: TrackableSetting, value: Any
    ) -> None:
        """Remove a pending request (e.g. after a failed service call)."""
        requests = self._pending_setting_requests[setting]
        tolerance = (
            self._pending_request_tolerance() if setting.is_numeric else None
        )
        for i, (pending, _ts) in enumerate(requests):
            if setting.values_match(value, pending, tolerance):
                del requests[i]
                break

    def _cleanup_pending_requests(
        self, setting: TrackableSetting
    ) -> None:
        """Remove expired pending requests for *setting*."""
        now = time.monotonic()
        requests = self._pending_setting_requests[setting]
        self._pending_setting_requests[setting] = [
            (v, ts) for v, ts in requests if now - ts < PENDING_REQUEST_TIMEOUT
        ]

    # ---- SSOT baseline helpers ----

    def _get_ssot_baseline(self, setting: TrackableSetting) -> Any:
        """Return the known-good baseline value for *setting*."""
        if setting == TrackableSetting.TEMPERATURE:
            return self._last_real_target_temp  # Alias — used extensively
        return self._ssot_baselines.get(setting)

    def _set_ssot_baseline(
        self, setting: TrackableSetting, value: Any
    ) -> None:
        """Update the known-good baseline for *setting*."""
        if setting == TrackableSetting.TEMPERATURE:
            self._last_real_target_temp = value  # Alias — used extensively
        elif setting == TrackableSetting.HVAC_MODE:
            self._ssot_baselines[setting] = value
            self._remember_non_off_hvac_mode(value)
        else:
            self._ssot_baselines[setting] = value

    def _remember_non_off_hvac_mode(self, mode: Any) -> None:
        """Keep track of the latest known non-OFF HVAC mode for turn_on."""
        if mode is None:
            return
        try:
            hvac_mode = HVACMode(mode)
        except ValueError:
            return
        if hvac_mode != HVACMode.OFF:
            self._last_non_off_hvac_mode = hvac_mode

    def _seed_ssot_baselines(self, state: State) -> None:
        """Seed all SSOT baselines from the first valid device state."""
        for setting in TrackableSetting:
            if setting == TrackableSetting.TEMPERATURE:
                # Only seed temperature if we don't already have a restored value.
                if self._last_real_target_temp is None:
                    seed_target = _coerce_temperature(
                        state.attributes.get(ATTR_TEMPERATURE)
                    )
                    if seed_target is not None:
                        self._last_real_target_temp = seed_target
            else:
                value = setting.read_from(state)
                if value is not None:
                    self._ssot_baselines[setting] = value

    # ---- Deterministic echo detection ----

    def _is_echo_of_our_change(self, new_state: State) -> bool:
        """Return True if *new_state* is an echo of a command we sent.

        An echo is an event where EVERY changed attribute either matches
        its SSOT baseline or a pending request we sent.  Checked across
        ALL active tracked settings regardless of SSOT/IT config.
        """
        for setting in self._active_tracked_settings:
            incoming = setting.read_from(new_state)
            baseline = self._get_ssot_baseline(setting)
            if incoming is None or baseline is None:
                continue  # Can't evaluate unknown attributes
            if setting.values_match(incoming, baseline):
                continue  # Matches baseline — no change
            if self._has_pending_setting_request(setting, incoming):
                continue  # Matches a pending request we sent
            return False  # Changed and NOT our echo
        return True

    def _consume_echo_pending_requests(self, new_state: State) -> None:
        """Consume pending requests that match the incoming echo state."""
        for setting in self._active_tracked_settings:
            incoming = setting.read_from(new_state)
            if incoming is not None:
                self._consume_pending_setting_request(setting, incoming)

    # ---- Backward-compat thin wrappers for temperature pending requests ----
    # These delegate to the generic system so that Section B (target temperature
    # tracking with strict/loose tolerance) continues to work unchanged.

    @property
    def _last_requested_real_target(self) -> float | None:
        requests = self._pending_setting_requests.get(
            TrackableSetting.TEMPERATURE, []
        )
        return requests[-1][0] if requests else None

    @_last_requested_real_target.setter
    def _last_requested_real_target(self, value: float | None) -> None:
        pass  # No-op: now derived from pending requests list

    def _record_real_target_request(self, real_target: float) -> None:
        """Track target values we have explicitly requested from the thermostat."""
        self._record_setting_request(TrackableSetting.TEMPERATURE, real_target)

    def _pending_request_tolerance(self) -> float:
        """Return the tolerance used when matching pending requests."""
        precision = self.precision or DEFAULT_PRECISION
        return max(
            PENDING_REQUEST_TOLERANCE_MIN,
            min(PENDING_REQUEST_TOLERANCE_MAX, precision / 2),
        )

    def _remove_real_target_request(self, real_target: float) -> None:
        """Remove a pending request after failures so we don't ignore real updates."""
        self._remove_pending_setting_request(
            TrackableSetting.TEMPERATURE, real_target
        )

    def _cleanup_expired_pending_requests(self) -> None:
        """Remove pending requests older than PENDING_REQUEST_TIMEOUT."""
        self._cleanup_pending_requests(TrackableSetting.TEMPERATURE)

    def _consume_real_target_request(
        self, real_target: float, tolerance: float
    ) -> bool:
        """Return True if a state update matches one of our pending requests."""
        return self._consume_pending_setting_request(
            TrackableSetting.TEMPERATURE, real_target, tolerance
        )

    def _has_pending_real_target_request(
        self, real_target: float, tolerance: float
    ) -> bool:
        """Return True if we've already asked the thermostat for this target."""
        return self._has_pending_setting_request(
            TrackableSetting.TEMPERATURE, real_target, tolerance
        )

    def _discover_temperature_unit(self) -> str:
        if self._real_state and (unit := self._real_state.attributes.get("unit_of_measurement")):
            return unit
        return self.hass.config.units.temperature_unit or UnitOfTemperature.CELSIUS

    def _get_real_current_temperature(self) -> float | None:
        if not self._real_state:
            self._mark_entity_health(self._real_entity_id, False)
            return None
        if self._real_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._mark_entity_health(self._real_entity_id, False)
            return None
        value = _coerce_temperature(
            self._real_state.attributes.get(ATTR_CURRENT_TEMPERATURE)
        )
        self._mark_entity_health(self._real_entity_id, value is not None)
        return value

    def _get_real_target_temperature(self) -> float | None:
        if not self._real_state:
            self._mark_entity_health(self._real_entity_id, False)
            return None
        value = _coerce_temperature(self._real_state.attributes.get(ATTR_TEMPERATURE))
        if value is None:
            self._mark_entity_health(self._real_entity_id, False)
        else:
            self._mark_entity_health(self._real_entity_id, True)
        return value

    def _get_real_current_humidity(self) -> float | None:
        if not self._real_state:
            return None
        return self._real_state.attributes.get(ATTR_CURRENT_HUMIDITY)

    def _get_active_sensor_temperature(self) -> float | None:
        sensor = self._sensor_lookup.get(self._selected_sensor_name)
        if not sensor:
            return None
        if sensor.is_physical:
            return self._get_real_current_temperature()
        state = self._sensor_states.get(sensor.entity_id)
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._mark_entity_health(sensor.entity_id, False)
            return None
        value = _coerce_temperature(state.state)
        if value is None:
            self._mark_entity_health(sensor.entity_id, False)
            return None
        self._mark_entity_health(sensor.entity_id, True)
        return value

    def _sync_virtual_target_from_real(self, real_target: float) -> float | None:
        sensor_temp = self._get_active_sensor_temperature()
        real_current = self._get_real_current_temperature()
        fallback = self._virtual_target_temperature
        if sensor_temp is None:
            sensor_temp = real_current
        if sensor_temp is None or real_current is None:
            return None
        derived = sensor_temp + (real_target - real_current)
        new_target = (
            self._apply_target_constraints(derived) if derived is not None else fallback
        )
        if new_target is None:
            return None

        previous_target = self._virtual_target_temperature
        tolerance = max(self.precision or DEFAULT_PRECISION, 0.1)
        if previous_target is not None and math.isclose(
            previous_target, new_target, abs_tol=tolerance
        ):
            return None

        self._virtual_target_temperature = new_target
        return new_target

    # --- Backward-compat boolean properties for SSOT / Ignore-Thermostat ---

    @property
    def _single_source_of_truth(self) -> bool:
        """True if any settings are SSOT-tracked."""
        return bool(self._ssot_settings)

    @property
    def _ignore_thermostat(self) -> bool:
        """True if any settings are in ignore-thermostat mode."""
        return bool(self._it_settings)

    @property
    def temperature_unit(self) -> str:
        return self._temperature_unit or self.hass.config.units.temperature_unit

    @property
    def min_temp(self) -> float:
        if self._user_min_temp is not None:
            return self._user_min_temp
        if self._min_temp is not None:
            return self._min_temp
        return super().min_temp

    @property
    def max_temp(self) -> float:
        if self._user_max_temp is not None:
            return self._user_max_temp
        if self._max_temp is not None:
            return self._max_temp
        return super().max_temp

    @property
    def target_temperature_step(self) -> float | None:
        if self._target_temp_step is not None:
            return self._target_temp_step
        if self._precision_override is not None:
            return self._precision_override
        return super().target_temperature_step

    @property
    def precision(self) -> float:
        if self._precision_override is not None:
            return self._precision_override
        if self._target_temp_step is not None:
            return self._target_temp_step
        return super().precision

    @property
    def current_temperature(self) -> float | None:
        return self._get_active_sensor_temperature() or self._get_real_current_temperature()

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity from the real thermostat."""
        return self._get_real_current_humidity()

    @property
    def target_temperature(self) -> float | None:
        if self._is_range_mode_active() and self._range_targets_available():
            return None
        if self._virtual_target_temperature is None:
            self._virtual_target_temperature = self._apply_target_constraints(
                self._last_real_target_temp
                or self._get_real_target_temperature()
                or self._get_active_sensor_temperature()
                or self._get_real_current_temperature()
            )
        return self._virtual_target_temperature

    def _is_range_mode_active(self) -> bool:
        if not self._real_state:
            return False
        supported = self._real_state.attributes.get("supported_features", 0)
        if not (supported & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE):
            return False
        return self.hvac_mode == HVACMode.HEAT_COOL

    def _range_targets_available(self) -> bool:
        high = self._get_it_or_real(TrackableSetting.TARGET_TEMP_HIGH)
        low = self._get_it_or_real(TrackableSetting.TARGET_TEMP_LOW)
        return high is not None and low is not None

    def _get_it_or_real(self, setting: TrackableSetting) -> Any:
        """Return IT baseline if locked, otherwise read from the real device."""
        if setting in self._it_settings:
            baseline = self._get_ssot_baseline(setting)
            if baseline is not None:
                return baseline
        if not self._real_state:
            return None
        return setting.read_from(self._real_state)

    @property
    def target_temperature_high(self) -> float | None:
        """Return the high target temperature for range mode."""
        if not self._is_range_mode_active():
            return None
        value = self._get_it_or_real(TrackableSetting.TARGET_TEMP_HIGH)
        if value is None:
            return self._virtual_target_temperature
        return value

    @property
    def target_temperature_low(self) -> float | None:
        """Return the low target temperature for range mode."""
        if not self._is_range_mode_active():
            return None
        value = self._get_it_or_real(TrackableSetting.TARGET_TEMP_LOW)
        if value is None:
            return self._virtual_target_temperature
        return value

    @property
    def target_humidity(self) -> float | None:
        """Return the target humidity from the real thermostat."""
        return self._get_it_or_real(TrackableSetting.TARGET_HUMIDITY)

    def _get_real_float_attr(self, key: str, default: float) -> float:
        """Read a float attribute from the real device, with fallback."""
        if self._real_state:
            val = self._real_state.attributes.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return default

    @property
    def min_humidity(self) -> float:
        """Return the minimum humidity, forwarded from the real device."""
        return self._get_real_float_attr("min_humidity", super().min_humidity)

    @property
    def max_humidity(self) -> float:
        """Return the maximum humidity, forwarded from the real device."""
        return self._get_real_float_attr("max_humidity", super().max_humidity)

    @property
    def hvac_mode(self) -> HVACMode | None:
        raw = self._get_it_or_real(TrackableSetting.HVAC_MODE)
        if raw is not None:
            try:
                return HVACMode(raw)
            except ValueError:
                return None
        return None

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action (heating, cooling, idle, etc.)."""
        if not self._real_state:
            return None
        action = self._real_state.attributes.get(ATTR_HVAC_ACTION)
        if action is not None:
            try:
                return HVACAction(action)
            except ValueError:
                return None
        return None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        if not self._real_state:
            return []
        modes = self._real_state.attributes.get("hvac_modes")
        if not isinstance(modes, list):
            return []
        result: list[HVACMode] = []
        for mode in modes:
            try:
                result.append(HVACMode(mode))
            except ValueError:
                continue
        return result

    @property
    def preset_modes(self) -> list[str] | None:
        return [sensor.name for sensor in self._sensors]

    @property
    def preset_mode(self) -> str | None:
        return self._selected_sensor_name

    @property
    def available(self) -> bool:
        # Keep proxy available after startup even if the physical entity is
        # temporarily unavailable; health is exposed via attributes.
        if not self._startup_complete and not self._real_state:
            return False
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._real_state:
            forwarded = {
                key: value
                for key, value in self._real_state.attributes.items()
                if key not in _RESERVED_REAL_ATTRIBUTES
            }
            attrs.update(forwarded)
        sensor = self._sensor_lookup.get(self._selected_sensor_name)
        attrs.update(
            {
                ATTR_ACTIVE_SENSOR: self._selected_sensor_name,
                ATTR_ACTIVE_SENSOR_ENTITY_ID: sensor.entity_id if sensor else None,
                ATTR_REAL_CURRENT_TEMPERATURE: self._get_real_current_temperature(),
                ATTR_REAL_TARGET_TEMPERATURE: self._last_real_target_temp
                or self._get_real_target_temperature(),
                ATTR_REAL_CURRENT_HUMIDITY: self._get_real_current_humidity(),
                ATTR_SELECTED_SENSOR_OPTIONS: {
                    item.name: (
                        self._real_entity_id if item.is_physical else item.entity_id
                    )
                    for item in self._sensors
                },
                "real_entity_available": bool(
                    self._real_state
                    and self._real_state.state
                    not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                ),
                "startup_complete": self._startup_complete,
                ATTR_UNAVAILABLE_ENTITIES: sorted(
                    entity
                    for entity, healthy in self._entity_health.items()
                    if not healthy
                ),
            }
        )
        if self._single_source_of_truth:
            for setting in _SSOT_EXPORTABLE_SETTINGS:
                attrs[setting.ssot_export_key] = self._get_ssot_baseline(setting)
            attrs[ATTR_IGNORE_THERMOSTAT] = self._ignore_thermostat
        return attrs

    def _compute_delta_target(self, requested: float) -> tuple[float, float, float, float] | None:
        """Compute the real target from a virtual requested temperature.

        Returns (constrained_virtual, real_target, display_current, real_current)
        or None if the computation cannot be performed.
        """
        constrained = self._apply_target_constraints(requested)
        if requested != constrained:
            _LOGGER.info(
                "%s target adjusted from %s to %s to honor thermostat limits",
                self.entity_id,
                requested,
                constrained,
            )

        display_current = self.current_temperature
        real_current = self._get_real_current_temperature()
        if display_current is None or real_current is None:
            _LOGGER.warning(
                "Cannot compute temperature delta for %s because sensor or thermostat is missing",
                self.entity_id,
            )
            return None

        delta = constrained - display_current
        calculated = real_current + delta
        clamped = self._apply_safety_clamp(calculated)
        if clamped is None:
            _LOGGER.warning(
                "Cannot set temperature for %s: safety clamp returned None",
                self.entity_id,
            )
            return None
        real_target = self._apply_target_constraints(clamped)
        return (constrained, real_target, display_current, real_current)

    def _begin_write_transaction(
        self,
        *,
        pending_updates: list[tuple[TrackableSetting, Any]],
        operation: str = "write",
        canonical_updates: list[tuple[TrackableSetting, Any]] | None = None,
        optimistic_real_target: float | None = None,
        suppress_auto_sync: bool = False,
    ) -> dict[str, Any]:
        """Apply shared pre-service write stages and return rollback snapshot."""
        snapshot = {
            "last_real_target_temp": self._last_real_target_temp,
            "last_real_write_time": self._last_real_write_time,
            "suppress_sync_logs_until": self._suppress_sync_logs_until,
            "ssot_baselines": {},
        }

        updates = canonical_updates or []
        for setting, value in updates:
            snapshot["ssot_baselines"][setting] = self._get_ssot_baseline(setting)
            self._set_ssot_baseline(setting, value)

        for setting, value in pending_updates:
            self._record_setting_request(setting, value)

        if optimistic_real_target is not None:
            self._last_real_target_temp = optimistic_real_target

        self._last_real_write_time = time.monotonic()
        if suppress_auto_sync:
            self._start_auto_sync_log_suppression()

        context_id = getattr(self._context, "id", None)
        context_user = getattr(self._context, "user_id", None)
        pending_snapshot = {
            setting.attr_key: [v for v, _ts in self._pending_setting_requests[setting]]
            for setting, _value in pending_updates
        }
        _LOGGER.debug(
            "%s started for %s (context_id=%r user_id=%r pending_updates=%r pending_snapshot=%r)",
            operation,
            self.entity_id,
            context_id,
            context_user,
            [(s.attr_key, v) for s, v in pending_updates],
            pending_snapshot,
        )

        return snapshot

    def _rollback_write_transaction(
        self,
        *,
        snapshot: dict[str, Any],
        pending_updates: list[tuple[TrackableSetting, Any]],
    ) -> None:
        """Rollback shared write stages after a service-call failure."""
        self._last_real_target_temp = snapshot["last_real_target_temp"]
        self._last_real_write_time = snapshot["last_real_write_time"]
        self._suppress_sync_logs_until = snapshot["suppress_sync_logs_until"]

        for setting, baseline in snapshot["ssot_baselines"].items():
            if setting == TrackableSetting.TEMPERATURE:
                self._last_real_target_temp = baseline
                continue
            if baseline is None:
                self._ssot_baselines.pop(setting, None)
            else:
                self._ssot_baselines[setting] = baseline

        for setting, value in pending_updates:
            self._remove_pending_setting_request(setting, value)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        async with self._command_lock:
            # --- Range mode (target_temp_high / target_temp_low) ---
            raw_high = kwargs.get("target_temp_high")
            raw_low = kwargs.get("target_temp_low")
            req_high = _coerce_temperature(raw_high)
            req_low = _coerce_temperature(raw_low)

            if req_high is not None or req_low is not None:
                if req_high is None:
                    req_high = _coerce_temperature(self.target_temperature_high)
                if req_low is None:
                    req_low = _coerce_temperature(self.target_temperature_low)
                if req_high is None or req_low is None:
                    _LOGGER.warning(
                        "Set temperature range called without complete high/low values for %s",
                        self.entity_id,
                    )
                    return
                result_high = self._compute_delta_target(req_high)
                result_low = self._compute_delta_target(req_low)
                if result_high is None or result_low is None:
                    return

                _, real_high, _, _ = result_high
                _, real_low, _, _ = result_low

                payload: dict[str, Any] = {
                    ATTR_ENTITY_ID: self._real_entity_id,
                    "target_temp_high": real_high,
                    "target_temp_low": real_low,
                }
                if ATTR_HVAC_MODE in kwargs and kwargs[ATTR_HVAC_MODE] is not None:
                    payload[ATTR_HVAC_MODE] = kwargs[ATTR_HVAC_MODE]

                pending_updates = [
                    (TrackableSetting.TARGET_TEMP_HIGH, real_high),
                    (TrackableSetting.TARGET_TEMP_LOW, real_low),
                ]
                canonical_updates = [
                    (TrackableSetting.TARGET_TEMP_HIGH, real_high),
                    (TrackableSetting.TARGET_TEMP_LOW, real_low),
                ] if self._single_source_of_truth else []
                snapshot = self._begin_write_transaction(
                    pending_updates=pending_updates,
                    operation="set_temperature_range",
                    canonical_updates=canonical_updates,
                    suppress_auto_sync=True,
                )
                try:
                    await self.hass.services.async_call(
                        CLIMATE_DOMAIN,
                        SERVICE_SET_TEMPERATURE,
                        payload,
                        blocking=True,
                    )
                except Exception:
                    self._rollback_write_transaction(
                        snapshot=snapshot,
                        pending_updates=pending_updates,
                    )
                    raise
                self.async_write_ha_state()
                return

            # --- Single target mode ---
            temperature = kwargs.get(ATTR_TEMPERATURE)
            requested = _coerce_temperature(temperature)
            if requested is None:
                _LOGGER.warning(
                    "Set temperature called with invalid value '%s' for %s",
                    temperature,
                    self.entity_id,
                )
                return

            result = self._compute_delta_target(requested)
            if result is None:
                return
            constrained_target, real_target, display_current, real_current = result

            payload = {
                ATTR_ENTITY_ID: self._real_entity_id,
                ATTR_TEMPERATURE: real_target,
            }
            if ATTR_HVAC_MODE in kwargs and kwargs[ATTR_HVAC_MODE] is not None:
                payload[ATTR_HVAC_MODE] = kwargs[ATTR_HVAC_MODE]

            actor_name = await self._get_actor_name()
            await self._async_log_real_adjustment(
                desired_target=real_target,
                reason="proxy target set",
                virtual_target=constrained_target,
                sensor_temp=display_current,
                real_current=real_current,
                actor_name=actor_name,
            )
            pending_updates = [(TrackableSetting.TEMPERATURE, real_target)]
            canonical_updates = (
                [(TrackableSetting.TEMPERATURE, real_target)]
                if self._single_source_of_truth
                else []
            )
            snapshot = self._begin_write_transaction(
                pending_updates=pending_updates,
                operation="set_temperature",
                canonical_updates=canonical_updates,
                optimistic_real_target=real_target,
                suppress_auto_sync=True,
            )
            try:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    SERVICE_SET_TEMPERATURE,
                    payload,
                    blocking=True,
                )
            except Exception:
                self._rollback_write_transaction(
                    snapshot=snapshot,
                    pending_updates=pending_updates,
                )
                raise

            self._virtual_target_temperature = constrained_target
            self.async_write_ha_state()

    async def _async_forward_setting(
        self, setting: TrackableSetting, value: Any
    ) -> None:
        """Forward a simple setting change to the physical thermostat.

        Handles SSOT baseline update, echo detection recording, write-time
        tracking, and the service call with optimistic-update-then-rollback.
        NOT used for async_set_temperature (unique delta computation).
        """
        record_value = float(value) if setting.is_numeric else value
        pending_updates = [(setting, record_value)]
        canonical_updates = (
            [(setting, record_value)]
            if self._single_source_of_truth
            else []
        )
        snapshot = self._begin_write_transaction(
            pending_updates=pending_updates,
            operation=f"forward_{setting.attr_key}",
            canonical_updates=canonical_updates,
        )

        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                setting.service_name,
                {
                    ATTR_ENTITY_ID: self._real_entity_id,
                    setting.service_attr: value,
                },
                blocking=True,
            )
        except Exception:
            self._rollback_write_transaction(
                snapshot=snapshot,
                pending_updates=pending_updates,
            )
            raise

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new HVAC mode, forwarded to the physical thermostat."""
        await self._async_forward_setting(TrackableSetting.HVAC_MODE, hvac_mode)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode, forwarded to the physical thermostat."""
        await self._async_forward_setting(TrackableSetting.FAN_MODE, fan_mode)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing mode, forwarded to the physical thermostat."""
        await self._async_forward_setting(TrackableSetting.SWING_MODE, swing_mode)

    async def async_set_humidity(self, humidity: int) -> None:
        """Set new target humidity, forwarded to the physical thermostat."""
        await self._async_forward_setting(TrackableSetting.TARGET_HUMIDITY, humidity)

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        """Set new horizontal swing mode, forwarded to the physical thermostat."""
        await self._async_forward_setting(TrackableSetting.SWING_HORIZONTAL_MODE, swing_horizontal_mode)

    async def async_turn_on(self) -> None:
        """Turn on and track expected HVAC mode like a mode-setting operation."""
        expected_mode = (
            self._last_non_off_hvac_mode
            or self._get_ssot_baseline(TrackableSetting.HVAC_MODE)
        )
        if expected_mode in (None, HVACMode.OFF):
            for mode in self.hvac_modes:
                if mode != HVACMode.OFF:
                    expected_mode = mode
                    break

        pending_updates: list[tuple[TrackableSetting, Any]] = []
        canonical_updates: list[tuple[TrackableSetting, Any]] = []
        if expected_mode and expected_mode != HVACMode.OFF:
            pending_updates = [(TrackableSetting.HVAC_MODE, expected_mode)]
            if self._single_source_of_truth:
                canonical_updates = [(TrackableSetting.HVAC_MODE, expected_mode)]

        snapshot = self._begin_write_transaction(
            pending_updates=pending_updates,
            operation="turn_on",
            canonical_updates=canonical_updates,
        )
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                "turn_on",
                {ATTR_ENTITY_ID: self._real_entity_id},
                blocking=True,
            )
        except Exception:
            self._rollback_write_transaction(
                snapshot=snapshot,
                pending_updates=pending_updates,
            )
            raise

    async def async_turn_off(self) -> None:
        """Turn off and track expected HVAC mode transition to OFF."""
        pending_updates = [(TrackableSetting.HVAC_MODE, HVACMode.OFF)]
        canonical_updates = (
            [(TrackableSetting.HVAC_MODE, HVACMode.OFF)]
            if self._single_source_of_truth
            else []
        )
        snapshot = self._begin_write_transaction(
            pending_updates=pending_updates,
            operation="turn_off",
            canonical_updates=canonical_updates,
        )
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                "turn_off",
                {ATTR_ENTITY_ID: self._real_entity_id},
                blocking=True,
            )
        except Exception:
            self._rollback_write_transaction(
                snapshot=snapshot,
                pending_updates=pending_updates,
            )
            raise

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in self._sensor_lookup:
            raise ValueError(f"Unknown preset '{preset_mode}'")

        self._selected_sensor_name = preset_mode
        # Only rebuild the virtual target if we don't yet have a stored value (e.g. very first run).
        if self._virtual_target_temperature is None:
            real_target = self._last_real_target_temp or self._get_real_target_temperature()
            if real_target is not None:
                self._sync_virtual_target_from_real(real_target)
        await self._async_realign_real_target_from_sensor()
        self.async_write_ha_state()

        sensor = self._sensor_lookup.get(preset_mode)
        sensor_entity = None
        if sensor:
            sensor_entity = (
                self._real_entity_id if sensor.is_physical else sensor.entity_id
            )
        unit = self.temperature_unit or ""
        sensor_temp = self._get_active_sensor_temperature()
        sensor_display = self._format_log_temperature(sensor_temp)
        segments = [f"sensor_name={preset_mode}"]
        segments.append(f"sensor_entity={sensor_entity or 'unknown'}")
        if sensor_display is not None:
            segments.append(f"sensor_temperature={sensor_display}{unit}")

        actor_name = await self._get_actor_name()
        suffix = f" (by {actor_name})" if actor_name else ""

        await self.hass.services.async_call(
            LOGBOOK_DOMAIN,
            LOGBOOK_SERVICE_LOG,
            {
                "name": self.name,
                "entity_id": self.entity_id,
                "message": "Preset changed to '%s': %s%s"
                % (preset_mode, " | ".join(segments), suffix),
            },
            blocking=False,
        )

    async def _async_log_virtual_target_sync(
        self, virtual_target: float, real_target: float
    ) -> None:
        """Record a logbook entry when we auto-sync to the real thermostat."""

        unit = self.temperature_unit or ""
        sensor_temp = self._get_active_sensor_temperature()
        real_current = self._get_real_current_temperature()

        sensor_display = self._format_log_temperature(sensor_temp)
        virtual_display = self._format_log_temperature(virtual_target)
        real_target_display = self._format_log_temperature(real_target)
        real_current_display = self._format_log_temperature(real_current)

        sensor_val = self._round_log_temperature_value(sensor_temp)
        real_target_val = self._round_log_temperature_value(real_target)
        real_current_val = self._round_log_temperature_value(real_current)
        virtual_val = self._round_log_temperature_value(virtual_target)

        segments: list[str] = []
        if real_target_display is not None:
            segments.append(f"real_target={real_target_display}{unit}")
        if real_current_display is not None:
            segments.append(f"real_current_temperature={real_current_display}{unit}")
            real_math = self._format_math_real_to_virtual(
                real_target_val,
                real_current_val,
                unit,
            )
            if real_math:
                segments.append(real_math)
        if sensor_display is not None:
            segments.append(f"sensor_temperature={sensor_display}{unit}")
        if virtual_display is not None:
            virtual_math = self._format_math_sensor_plus_delta(
                sensor_val,
                real_target_val,
                real_current_val,
                virtual_val,
                unit,
            )
            if virtual_math:
                segments.append(virtual_math)
            segments.append(f"virtual_target={virtual_display}{unit}")
        if not segments:
            segments.append("no context available")

        await self.hass.services.async_call(
            LOGBOOK_DOMAIN,
            LOGBOOK_SERVICE_LOG,
            {
                "name": self.name,
                "entity_id": self.entity_id,
                "message": (
                    "Virtual target auto-synced after %s reported a new target: %s"
                    % (self._real_entity_id, " | ".join(segments))
                ),
            },
            blocking=False,
        )

    async def _async_log_physical_override(
        self, real_target: float | None, switched: bool
    ) -> None:
        """Record when an external change forces us to the physical preset."""

        unit = self.temperature_unit or ""
        real_target_display = self._format_log_temperature(real_target)
        target_segment = None
        if real_target_display is not None:
            target_segment = f"real_target={real_target_display}{unit}"

        segments = [
            f"source_entity={self._real_entity_id}",
            f"preset={self._physical_sensor_name}",
        ]
        if target_segment:
            segments.append(target_segment)

        action = "switched" if switched else "kept"

        await self.hass.services.async_call(
            LOGBOOK_DOMAIN,
            LOGBOOK_SERVICE_LOG,
            {
                "name": self.name,
                "entity_id": self.entity_id,
                "message": (
                    "Detected external target change; %s preset to '%s': %s"
                    % (action, self._physical_sensor_name, " | ".join(segments))
                ),
            },
            blocking=False,
        )

    def _start_auto_sync_log_suppression(self) -> None:
        """Temporarily silence auto-sync logs after commands we initiate."""

        self._suppress_sync_logs_until = time.monotonic() + 5

    def _should_log_auto_sync(self) -> bool:
        """Return True if we're outside the suppression window."""

        if self._suppress_sync_logs_until is None:
            return True
        if time.monotonic() >= self._suppress_sync_logs_until:
            self._suppress_sync_logs_until = None
            return True
        return False

    async def _async_realign_real_target_from_sensor(self, retry: bool = False) -> None:
        """Push a new target temperature to the real thermostat based on the active sensor."""

        if self._virtual_target_temperature is None:
            return
            
        now = time.monotonic()
        if self._cooldown_period > 0:
            time_since_last_write = now - self._last_real_write_time
            if time_since_last_write < self._cooldown_period:
                if self._cooldown_timer_unsub is None:
                    retry_delay = self._cooldown_period - time_since_last_write
                    _LOGGER.info(
                        "Update blocked by cooldown (%.1fs remaining). Scheduling retry in %.1fs",
                        self._cooldown_period - time_since_last_write,
                        retry_delay,
                    )
                    self._cooldown_timer_unsub = async_call_later(
                        self.hass, retry_delay, self._async_cooldown_retry
                    )
                return
        
        # If we proceed, clear any pending retry since we are acting now
        if self._cooldown_timer_unsub:
            self._cooldown_timer_unsub()
            self._cooldown_timer_unsub = None

        async with self._command_lock:
            sensor_temp = self._get_active_sensor_temperature()
            real_current = self._get_real_current_temperature()
            if sensor_temp is None or real_current is None:
                return

            delta = self._virtual_target_temperature - sensor_temp
            calculated_real_target = real_current + delta
            
            # Overdrive Logic: Check if we are stalled
            # Stalled = Target not met AND Real Thermostat is Idle
            overdrive_active = False
            overdrive_adjust = 0.0

            if self._real_state and self.hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
                 real_action = self._real_state.attributes.get(ATTR_HVAC_ACTION)
                 tolerance = max(self.precision or DEFAULT_PRECISION, 0.1)
                 
                 # Heat Mode Stall
                 if self.hvac_mode == HVACMode.HEAT:
                     # We want heat, but we aren't heating
                     want_heat = self._virtual_target_temperature > (sensor_temp + tolerance)
                     not_heating = real_action != HVACAction.HEATING
                     if want_heat and not_heating:
                         overdrive_active = True
                         # Push target up to force start
                         overdrive_adjust = OVERDRIVE_ADJUSTMENT_HEAT # Degree matching unit
                         _LOGGER.info("Overdrive active: Heating required but thermostat idle. Applying +%s offset.", overdrive_adjust)

                 # Cool Mode Stall
                 elif self.hvac_mode == HVACMode.COOL:
                     # We want cool, but we aren't cooling
                     want_cool = self._virtual_target_temperature < (sensor_temp - tolerance)
                     not_cooling = real_action != HVACAction.COOLING
                     if want_cool and not_cooling:
                         overdrive_active = True
                         # Push target down to force start
                         overdrive_adjust = OVERDRIVE_ADJUSTMENT_COOL
                         _LOGGER.info("Overdrive active: Cooling required but thermostat idle. Applying %s offset.", overdrive_adjust)
            
            if overdrive_active:
                calculated_real_target = calculated_real_target + overdrive_adjust
            
            desired_real_target = self._apply_safety_clamp(calculated_real_target)
            if desired_real_target is None:
                return
            desired_real_target = self._apply_target_constraints(desired_real_target)

            current_real_target = self._get_real_target_temperature()
            # We must be strict here; if the step is 1.0, 66 vs 67 must be seen as different.
            # Using self.precision (1.0) as tolerance caused isclose(66, 67, abs_tol=1.0) -> True.
            target_tolerance = 0.1
            
            # If we are in overdrive, we might be pushing AWAY from the "correct" delta-based target
            # So we should generally update if there's a difference.
            # But the standard check is:
            if current_real_target is not None and math.isclose(
                current_real_target, desired_real_target, abs_tol=target_tolerance
            ):
                return
            
            pending_tolerance = self._pending_request_tolerance()
            if self._has_pending_real_target_request(desired_real_target, pending_tolerance):
                return

            reason = "sensor realignment" + (" (overdrive)" if overdrive_active else "")
            if retry:
                reason += " (cooldown expired)"

            await self._async_log_real_adjustment(
                desired_target=desired_real_target,
                reason=reason,
                virtual_target=self._virtual_target_temperature,
                sensor_temp=sensor_temp,
                real_current=real_current,
                actor_name=None,
                overdrive_adjust=overdrive_adjust if overdrive_active else None,
            )
            pending_updates = [(TrackableSetting.TEMPERATURE, desired_real_target)]
            canonical_updates = (
                [(TrackableSetting.TEMPERATURE, desired_real_target)]
                if self._single_source_of_truth
                else []
            )
            snapshot = self._begin_write_transaction(
                pending_updates=pending_updates,
                operation="sensor_realign",
                canonical_updates=canonical_updates,
                optimistic_real_target=desired_real_target,
                suppress_auto_sync=True,
            )
            try:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    SERVICE_SET_TEMPERATURE,
                    {
                        ATTR_ENTITY_ID: self._real_entity_id,
                        ATTR_TEMPERATURE: desired_real_target,
                    },
                    blocking=True,
                )
            except Exception as err:
                self._rollback_write_transaction(
                    snapshot=snapshot,
                    pending_updates=pending_updates,
                )
                
                if "502" in str(err) or "Bad Gateway" in str(err):
                    _LOGGER.warning(
                        "Failed to set temperature on %s (502 Bad Gateway) - will retry on next sync",
                        self._real_entity_id,
                    )
                else:
                    _LOGGER.error(
                        "Error setting %s temperature to %s: %s",
                        self._real_entity_id,
                        desired_real_target,
                        err,
                    )


    @callback
    def _async_cooldown_retry(self, _now: datetime.datetime) -> None:
        """Retry the alignment after cooldown expires."""
        self._cooldown_timer_unsub = None
        self._schedule_target_realign(retry=True)

    async def _async_restore_state(self) -> None:
        last_state = await self.async_get_last_state()
        if not last_state:
            return

        restored_sensor = last_state.attributes.get(ATTR_ACTIVE_SENSOR)
        if self._use_last_active_sensor and restored_sensor in self._sensor_lookup:
            self._selected_sensor_name = restored_sensor
        elif self._configured_default_sensor:
            self._selected_sensor_name = self._configured_default_sensor
        elif restored_sensor in self._sensor_lookup:
            self._selected_sensor_name = restored_sensor

        restored_virtual = _coerce_temperature(last_state.attributes.get(ATTR_TEMPERATURE))
        if restored_virtual is not None:
            self._virtual_target_temperature = self._apply_target_constraints(
                restored_virtual
            )

        restored_real = _coerce_temperature(
            last_state.attributes.get(ATTR_REAL_TARGET_TEMPERATURE)
        )
        if restored_real is not None:
            self._last_real_target_temp = restored_real

        if self._single_source_of_truth:
            for setting in _SSOT_EXPORTABLE_SETTINGS:
                restored = last_state.attributes.get(setting.ssot_export_key)
                if restored is not None:
                    self._set_ssot_baseline(setting, restored)

    def _update_real_temperature_limits(self) -> None:
        if not self._real_state:
            self._min_temp = None
            self._max_temp = None
            self._target_temp_step = None
            self._precision_override = None
            self._mark_entity_health(self._real_entity_id, False)
            return

        is_available = self._real_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        self._mark_entity_health(self._real_entity_id, is_available)

        self._min_temp = _coerce_temperature(self._real_state.attributes.get(ATTR_MIN_TEMP))
        self._max_temp = _coerce_temperature(self._real_state.attributes.get(ATTR_MAX_TEMP))
        self._target_temp_step = _coerce_positive_float(
            self._real_state.attributes.get(ATTR_TARGET_TEMP_STEP)
        )
        real_precision = _coerce_positive_float(self._real_state.attributes.get("precision"))
        if real_precision is not None:
            self._precision_override = real_precision
        elif self._target_temp_step is not None:
            self._precision_override = self._target_temp_step
        else:
            self._precision_override = None

        # Mirror supported features from the real thermostat.
        supported = self._real_state.attributes.get("supported_features", 0)
        base_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
        )
        for flag in _PASSTHROUGH_FEATURES:
            if supported & flag:
                base_features |= flag
        for flag in _FEATURE_TO_SETTINGS:
            if supported & flag:
                base_features |= flag
        self._attr_supported_features = base_features

        # Populate active tracked settings based on device capabilities.
        active = set(_CORE_TRACKED_SETTINGS)
        for flag, settings in _FEATURE_TO_SETTINGS.items():
            if supported & flag:
                active.update(settings)
        self._active_tracked_settings = active

    def _update_sensor_health_from_state(self, entity_id: str | None, state: State | None) -> None:
        if not entity_id:
            return
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._mark_entity_health(entity_id, False)
            return
        self._mark_entity_health(entity_id, _coerce_temperature(state.state) is not None)

    def _mark_entity_health(self, entity_id: str | None, is_available: bool) -> None:
        if not entity_id:
            return
        previous = self._entity_health.get(entity_id)
        if previous == is_available:
            return
        self._entity_health[entity_id] = is_available
        # During startup discovery, missing states are common and not actionable.
        if (
            not self._startup_complete
            and previous is None
            and not is_available
        ):
            _LOGGER.debug(
                "Entity %s not yet available during startup for %s",
                entity_id,
                self.entity_id,
            )
            return
        if not is_available:
            _LOGGER.warning(
                "Entity %s became unavailable for %s; using fallbacks where possible",
                entity_id,
                self.entity_id,
            )
        elif previous is not None:
            _LOGGER.info(
                "Entity %s recovered for %s",
                entity_id,
                self.entity_id,
            )

    def _apply_target_constraints(self, value: float | None) -> float | None:
        if value is None:
            return None
        result = value
        min_temp = self.min_temp
        max_temp = self.max_temp
        if min_temp is not None:
            result = max(result, min_temp)
        if max_temp is not None:
            result = min(result, max_temp)
        step = self.target_temperature_step
        if step:
            try:
                if step > 0:
                    result = round(result / step) * step
            except TypeError:
                step = None
        if min_temp is not None:
            result = max(result, min_temp)
        if max_temp is not None:
            result = min(result, max_temp)
        return self._round_temperature(result)

    def _apply_safety_clamp(self, calculated_target: float | None) -> float | None:
        """Apply user-configured safety limits, falling back to physical thermostat limits."""
        if calculated_target is None:
            return None
        
        original_target = calculated_target
        clamped = False
        clamp_reason = None
        limit_value = None
        
        effective_min = self._user_min_temp if self._user_min_temp is not None else self._min_temp
        effective_max = self._user_max_temp if self._user_max_temp is not None else self._max_temp
        
        if effective_min is not None and effective_max is not None and effective_min > effective_max:
            _LOGGER.error(
                "Thermostat Proxy (%s): Invalid configuration - min_temp (%.1f) > max_temp (%.1f). "
                "Using max_temp as safety limit.",
                self.entity_id,
                effective_min,
                effective_max,
            )
            return effective_max
        
        if effective_min is not None and calculated_target < effective_min:
            calculated_target = effective_min
            clamped = True
            clamp_reason = "min"
            limit_value = effective_min
        elif effective_max is not None and calculated_target > effective_max:
            calculated_target = effective_max
            clamped = True
            clamp_reason = "max"
            limit_value = effective_max
        
        if clamped:
            unit = self.temperature_unit or ""
            limit_source = "user-configured" if (
                (clamp_reason == "max" and self._user_max_temp is not None) or
                (clamp_reason == "min" and self._user_min_temp is not None)
            ) else "physical thermostat"
            
            _LOGGER.warning(
                "Thermostat Proxy (%s): Calculated target %.1f%s exceeded %s limit %.1f%s. Clamping to %.1f%s (source: %s)",
                self.entity_id,
                original_target,
                unit,
                clamp_reason,
                limit_value,
                unit,
                calculated_target,
                unit,
                limit_source,
            )
        
        return calculated_target

    def _round_temperature(self, value: float) -> float:
        precision = self.precision or DEFAULT_PRECISION
        if precision >= 1:
            return round(value)
        if math.isclose(precision, 0.5, abs_tol=0.01):
            return round(value * 2) / 2

        decimals = max(1, min(3, int(round(-math.log10(precision)))))
        return round(value, decimals)

    def _add_physical_sensor(self, sensors: list[SensorConfig]) -> list[SensorConfig]:
        sensors_with_physical = list(sensors)
        if any(
            sensor.name == self._physical_sensor_name for sensor in sensors_with_physical
        ):
            _LOGGER.warning(
                "Sensor name '%s' is reserved for %s; skipping built-in physical sensor",
                self._physical_sensor_name,
                self.entity_id,
            )
            return sensors_with_physical

        sensors_with_physical.append(
            SensorConfig(
                name=self._physical_sensor_name,
                entity_id=PHYSICAL_SENSOR_SENTINEL,
                is_physical=True,
            )
        )
        return sensors_with_physical

    async def _async_log_real_adjustment(
        self,
        *,
        desired_target: float | None,
        reason: str,
        virtual_target: float | None,
        sensor_temp: float | None,
        real_current: float | None,
        actor_name: str | None = None,
        overdrive_adjust: float | None = None,
    ) -> None:
        if desired_target is None:
            return
        unit = self.temperature_unit or ""
        sensor_display = self._format_log_temperature(sensor_temp)
        virtual_display = self._format_log_temperature(virtual_target)
        real_display = self._format_log_temperature(real_current)
        sensor_val = self._round_log_temperature_value(sensor_temp)
        virtual_val = self._round_log_temperature_value(virtual_target)
        real_val = self._round_log_temperature_value(real_current)
        desired_val = self._round_log_temperature_value(desired_target)

        segments: list[str] = []
        if sensor_display is not None:
            segments.append(f"sensor_temperature={sensor_display}{unit}")
        if virtual_display is not None:
            segments.append(f"virtual_target={virtual_display}{unit}")
            sensor_math = self._format_math_sensor_virtual(sensor_val, virtual_val, unit)
            if sensor_math:
                segments.append(sensor_math)
        if real_display is not None:
            segments.append(f"real_current_temperature={real_display}{unit}")
            real_math = self._format_math_real_adjustment(
                real_val,
                sensor_val,
                virtual_val,
                desired_val,
                unit,
                overdrive_adjust,
            )
            if real_math:
                segments.append(real_math)
        if not segments:
            segments.append("no context available")

        suffix = f" (by {actor_name})" if actor_name else ""

        context_text = " | ".join(segments)
        message = (
            "Adjusted target on %s to %s%s%s (%s): %s"
            % (self._real_entity_id, desired_target, unit, suffix, reason, context_text)
        )
        _LOGGER.info("%s %s", self.entity_id, message)
        await self.hass.services.async_call(
            LOGBOOK_DOMAIN,
            LOGBOOK_SERVICE_LOG,
            {
                "name": self.name,
                "entity_id": self.entity_id,
                "message": message,
            },
            blocking=False,
        )

    def _format_log_temperature(self, value: float | None) -> str | None:
        rounded = self._round_log_temperature_value(value)
        if rounded is None:
            return None
        return str(rounded)

    def _round_log_temperature_value(self, value: float | None) -> int | None:
        if value is None:
            return None
        return int(round(value))

    def _format_math_sensor_virtual(
        self,
        sensor_val: int | None,
        virtual_val: int | None,
        unit: str,
    ) -> str | None:
        if sensor_val is None or virtual_val is None:
            return None
        diff = sensor_val - virtual_val
        return f"{sensor_val}{unit} - {virtual_val}{unit} = {diff}{unit}"

    def _format_math_real_adjustment(
        self,
        real_val: int | None,
        sensor_val: int | None,
        virtual_val: int | None,
        desired_val: int | None,
        unit: str,
        overdrive_adjust: float | None = None,
    ) -> str | None:
        if (
            real_val is None
            or sensor_val is None
            or virtual_val is None
        ):
            return None
        diff = sensor_val - virtual_val
        if diff >= 0:
            op = "-"
            delta = diff
        else:
            op = "+"
            delta = abs(diff)
        result = desired_val if desired_val is not None else real_val - diff
        if overdrive_adjust:
            round_adjust = int(round(overdrive_adjust))
            op_adj = "+" if round_adjust >= 0 else "-"
            return f"{real_val}{unit} {op} {delta}{unit} ({op_adj}{abs(round_adjust)} overdrive) = {result}{unit}"
        return f"{real_val}{unit} {op} {delta}{unit} = {result}{unit}"

    def _format_math_real_to_virtual(
        self,
        real_target_val: int | None,
        real_current_val: int | None,
        unit: str,
    ) -> str | None:
        if real_target_val is None or real_current_val is None:
            return None
        diff = real_target_val - real_current_val
        return f"{real_target_val}{unit} - {real_current_val}{unit} = {diff}{unit}"

    def _format_math_sensor_plus_delta(
        self,
        sensor_val: int | None,
        real_target_val: int | None,
        real_current_val: int | None,
        virtual_val: int | None,
        unit: str,
    ) -> str | None:
        if (
            sensor_val is None
            or real_target_val is None
            or real_current_val is None
        ):
            return None
        diff = real_target_val - real_current_val
        if diff >= 0:
            op = "+"
            delta = diff
        else:
            op = "-"
            delta = abs(diff)
        result = virtual_val if virtual_val is not None else sensor_val + diff
        return f"{sensor_val}{unit} {op} {delta}{unit} = {result}{unit}"

    async def _get_actor_name(self) -> str | None:
        """Attempt to identify the user who triggered the current action."""
        if not self._context or not self._context.user_id:
            return None
        
        user = await self.hass.auth.async_get_user(self._context.user_id)
        return user.name if user else None

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting."""
        return self._get_it_or_real(TrackableSetting.FAN_MODE)

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the list of available fan modes."""
        if self._real_state:
            return self._real_state.attributes.get("fan_modes")
        return None

    @property
    def swing_mode(self) -> str | None:
        """Return the swing setting."""
        return self._get_it_or_real(TrackableSetting.SWING_MODE)

    @property
    def swing_modes(self) -> list[str] | None:
        """Return the list of available swing modes."""
        if self._real_state:
            return self._real_state.attributes.get("swing_modes")
        return None

    @property
    def swing_horizontal_mode(self) -> str | None:
        """Return the horizontal swing setting."""
        return self._get_it_or_real(TrackableSetting.SWING_HORIZONTAL_MODE)

    @property
    def swing_horizontal_modes(self) -> list[str] | None:
        """Return the list of available horizontal swing modes."""
        if self._real_state:
            return self._real_state.attributes.get("swing_horizontal_modes")
        return None
