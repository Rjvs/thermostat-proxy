"""Shared thermostat proxy model/types/helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.core import State

DEFAULT_PRECISION = 0.1
PENDING_REQUEST_TOLERANCE_MIN = 0.05
PENDING_REQUEST_TOLERANCE_MAX = 0.5
MAX_TRACKED_REAL_TARGET_REQUESTS = 5
PENDING_REQUEST_TIMEOUT = 30.0  # Seconds before a pending request expires

# Attributes supplied by ClimateEntity itself that must NOT be overridden by
# forwarding the physical thermostat's attributes, otherwise the front-end sees
# the wrong preset/temperature metadata.
_RESERVED_REAL_ATTRIBUTES = {
    "temperature",
    "target_temp_high",
    "target_temp_low",
    "current_temperature",
    "hvac_modes",
    "hvac_mode",
    "hvac_action",
    "preset_modes",
    "preset_mode",
    "target_temp_step",
    "supported_features",
    "fan_mode",
    "fan_modes",
    "swing_mode",
    "swing_modes",
    "swing_horizontal_mode",
    "swing_horizontal_modes",
    "current_humidity",
    "humidity",
    "min_humidity",
    "max_humidity",
    "min_temp",
    "max_temp",
    "precision",
}


class TrackableSetting(Enum):
    """Writable climate attributes tracked for echo detection and SSOT."""

    # fmt: off
    #                                attr_key               is_state is_numeric service_name                service_attr              ssot_export_key               correction_group  state_key  feature_flag                                    label
    HVAC_MODE             = ("hvac_mode",                    True,    False,     "set_hvac_mode",            "hvac_mode",              "ssot_hvac_mode",             None,             None,      None,                                           "HVAC mode")
    TEMPERATURE           = ("temperature",                  False,   True,      "set_temperature",          "temperature",            None,                         None,             None,      None,                                           "Temperature")
    FAN_MODE              = ("fan_mode",                     False,   False,     "set_fan_mode",             "fan_mode",               "ssot_fan_mode",              None,             None,      ClimateEntityFeature.FAN_MODE,                  "Fan mode")
    SWING_MODE            = ("swing_mode",                   False,   False,     "set_swing_mode",           "swing_mode",             "ssot_swing_mode",            None,             None,      ClimateEntityFeature.SWING_MODE,                "Swing mode")
    TARGET_TEMP_HIGH      = ("target_temp_high",             False,   True,      "set_temperature",          "target_temp_high",       "ssot_target_temp_high",      "temp_range",     None,      ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,  "Target temp high")
    TARGET_TEMP_LOW       = ("target_temp_low",              False,   True,      "set_temperature",          "target_temp_low",        "ssot_target_temp_low",       "temp_range",     None,      ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,  "Target temp low")
    TARGET_HUMIDITY       = ("target_humidity",              False,   True,      "set_humidity",             "humidity",               "ssot_target_humidity",       None,             "humidity", ClimateEntityFeature.TARGET_HUMIDITY,           "Target humidity")
    SWING_HORIZONTAL_MODE = ("swing_horizontal_mode",        False,   False,     "set_swing_horizontal_mode","swing_horizontal_mode",  "ssot_swing_horizontal_mode", None,             None,      ClimateEntityFeature.SWING_HORIZONTAL_MODE,     "Swing horizontal mode")
    # fmt: on

    def __init__(
        self,
        attr_key: str,
        is_state: bool,
        is_numeric: bool,
        service_name: str,
        service_attr: str,
        ssot_export_key: str | None,
        correction_group: str | None,
        state_key_override: str | None,
        feature_flag: ClimateEntityFeature | None,
        label: str,
    ) -> None:
        self.attr_key = attr_key
        self.is_state = is_state
        self.is_numeric = is_numeric
        self.service_name = service_name
        self.service_attr = service_attr
        self.ssot_export_key = ssot_export_key
        self.correction_group = correction_group
        self._state_key = state_key_override
        self.feature_flag = feature_flag
        self.label = label

    @property
    def state_key(self) -> str:
        """Key used to read this setting from state.attributes."""
        return self._state_key if self._state_key is not None else self.attr_key

    def read_from(self, state: State) -> Any:
        """Extract this setting's value from a HA State object."""
        if self.is_state:
            return state.state
        raw = state.attributes.get(self.state_key)
        if self.is_numeric:
            return _coerce_temperature(raw)
        return raw

    def values_match(
        self, a: Any, b: Any, tolerance: float | None = None
    ) -> bool:
        """Compare two values for this setting type."""
        if a is None or b is None:
            return a is b
        if self.is_numeric:
            tol = (
                tolerance
                if tolerance is not None
                else PENDING_REQUEST_TOLERANCE_MAX
            )
            return math.isclose(a, b, abs_tol=tol)
        return self._normalize_discrete(a) == self._normalize_discrete(b)

    @staticmethod
    def _normalize_discrete(value: Any) -> Any:
        """Normalize enums and other wrappers for deterministic equality."""
        if isinstance(value, Enum):
            return value.value
        return value


_TRACKABLE_SETTING_BY_KEY: dict[str, TrackableSetting] = {
    s.attr_key: s for s in TrackableSetting
}

_SSOT_EXPORTABLE_SETTINGS: tuple[TrackableSetting, ...] = tuple(
    s for s in TrackableSetting if s.ssot_export_key is not None
)

_CORRECTION_GROUPS: dict[str, list[TrackableSetting]] = {}
for _s in TrackableSetting:
    if _s.correction_group:
        _CORRECTION_GROUPS.setdefault(_s.correction_group, []).append(_s)

_CORE_TRACKED_SETTINGS: set[TrackableSetting] = {
    TrackableSetting.HVAC_MODE,
    TrackableSetting.TEMPERATURE,
}

_FEATURE_TO_SETTINGS: dict[ClimateEntityFeature, tuple[TrackableSetting, ...]] = {}
for _s in TrackableSetting:
    if _s.feature_flag is not None:
        _FEATURE_TO_SETTINGS.setdefault(_s.feature_flag, []).append(_s)
_FEATURE_TO_SETTINGS = {k: tuple(v) for k, v in _FEATURE_TO_SETTINGS.items()}

_PASSTHROUGH_FEATURES: frozenset[ClimateEntityFeature] = frozenset({
    ClimateEntityFeature.TURN_ON,
    ClimateEntityFeature.TURN_OFF,
})


@dataclass
class SensorConfig:
    """Configuration for a temperature sensor."""

    name: str
    entity_id: str | None
    is_physical: bool = False


def _coerce_temperature(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _coerce_positive_float(value: Any) -> float | None:
    result = _coerce_temperature(value)
    if result is None or result <= 0:
        return None
    return result
