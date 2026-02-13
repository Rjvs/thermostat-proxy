"""Thermostat Proxy climate platform compatibility facade.

Home Assistant loads this module as the climate platform entrypoint.
Implementation is split across:
- climate_platform.py (schema + setup entrypoints)
- climate_entity.py (entity behavior + helpers)
"""

from .climate_entity import *  # noqa: F401,F403
from .climate_entity import (
    _CORRECTION_GROUPS,
    _CORE_TRACKED_SETTINGS,
    _FEATURE_TO_SETTINGS,
    _PASSTHROUGH_FEATURES,
    _RESERVED_REAL_ATTRIBUTES,
    _SSOT_EXPORTABLE_SETTINGS,
    _TRACKABLE_SETTING_BY_KEY,
    _coerce_positive_float,
    _coerce_temperature,
)
from .climate_platform import PLATFORM_SCHEMA, async_setup_entry, async_setup_platform
