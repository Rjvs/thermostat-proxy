"""Thermostat Proxy climate platform setup functions."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .climate_entity import CustomThermostatEntity
from .const import (
    CONF_COOLDOWN_PERIOD,
    CONF_DEFAULT_SENSOR,
    CONF_IGNORE_THERMOSTAT,
    CONF_IT_SETTINGS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_PHYSICAL_SENSOR_NAME,
    CONF_SENSOR_ENTITY_ID,
    CONF_SENSOR_NAME,
    CONF_SENSORS,
    CONF_SINGLE_SOURCE_OF_TRUTH,
    CONF_SSOT_SETTINGS,
    CONF_THERMOSTAT,
    CONF_UNIQUE_ID,
    CONF_USE_LAST_ACTIVE_SENSOR,
    DEFAULT_COOLDOWN_PERIOD,
    DEFAULT_NAME,
    DEFAULT_SENSOR_LAST_ACTIVE,
    PHYSICAL_SENSOR_NAME,
)

_LOGGER = logging.getLogger(__name__)

SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SENSOR_NAME): cv.string,
        vol.Required(CONF_SENSOR_ENTITY_ID): cv.entity_id,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_THERMOSTAT): cv.entity_id,
        vol.Required(CONF_SENSORS): vol.All(cv.ensure_list, vol.Length(min=1), [SENSOR_SCHEMA]),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_DEFAULT_SENSOR): cv.string,
        vol.Optional(CONF_PHYSICAL_SENSOR_NAME): cv.string,
        vol.Optional(CONF_USE_LAST_ACTIVE_SENSOR, default=False): cv.boolean,
        vol.Optional(CONF_COOLDOWN_PERIOD, default=DEFAULT_COOLDOWN_PERIOD): vol.All(
            cv.time_period, cv.positive_timedelta
        ),
        vol.Optional(CONF_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_SINGLE_SOURCE_OF_TRUTH, default=False): cv.boolean,
        vol.Optional(CONF_IGNORE_THERMOSTAT, default=False): cv.boolean,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up a Thermostat Proxy entity from YAML."""

    default_sensor = config.get(CONF_DEFAULT_SENSOR)
    use_last_active_sensor = config.get(CONF_USE_LAST_ACTIVE_SENSOR, False)
    if default_sensor == DEFAULT_SENSOR_LAST_ACTIVE:
        use_last_active_sensor = True
        default_sensor = None

    async_add_entities(
        [
            CustomThermostatEntity(
                hass=hass,
                name=config[CONF_NAME],
                real_thermostat=config[CONF_THERMOSTAT],
                sensors=config[CONF_SENSORS],
                default_sensor=default_sensor,
                unique_id=config.get(CONF_UNIQUE_ID),
                physical_sensor_name=config.get(
                    CONF_PHYSICAL_SENSOR_NAME, PHYSICAL_SENSOR_NAME
                ),
                use_last_active_sensor=use_last_active_sensor,
                cooldown_period=config.get(CONF_COOLDOWN_PERIOD, DEFAULT_COOLDOWN_PERIOD),
                user_min_temp=config.get(CONF_MIN_TEMP),
                user_max_temp=config.get(CONF_MAX_TEMP),
                single_source_of_truth=config.get(CONF_SINGLE_SOURCE_OF_TRUTH, False),
                ignore_thermostat=config.get(CONF_IGNORE_THERMOSTAT, False),
                ssot_settings=config.get(CONF_SSOT_SETTINGS),
                it_settings=config.get(CONF_IT_SETTINGS),
            )
        ]
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a Thermostat Proxy entity from a config entry."""

    def _opt(key: str, default: Any = None) -> Any:
        """Read config value from options, falling back to data."""
        return entry.options.get(key, entry.data.get(key, default))

    data = entry.data
    sensors = data.get(CONF_SENSORS) or []
    if not sensors:
        _LOGGER.error(
            "Config entry %s is missing sensors; skipping Thermostat Proxy creation",
            entry.entry_id,
        )
        return

    raw_default_sensor = _opt(CONF_DEFAULT_SENSOR)
    physical_sensor_name = data.get(CONF_PHYSICAL_SENSOR_NAME, PHYSICAL_SENSOR_NAME)
    valid_sensor_names = [sensor[CONF_SENSOR_NAME] for sensor in sensors]
    if physical_sensor_name not in valid_sensor_names:
        valid_sensor_names.append(physical_sensor_name)

    use_last_active_sensor = _opt(CONF_USE_LAST_ACTIVE_SENSOR, False)
    cooldown_period = _opt(CONF_COOLDOWN_PERIOD, DEFAULT_COOLDOWN_PERIOD)
    user_min_temp = _opt(CONF_MIN_TEMP)
    user_max_temp = _opt(CONF_MAX_TEMP)
    single_source_of_truth = _opt(CONF_SINGLE_SOURCE_OF_TRUTH, False)
    ignore_thermostat = _opt(CONF_IGNORE_THERMOSTAT, False)
    ssot_settings = _opt(CONF_SSOT_SETTINGS)
    it_settings = _opt(CONF_IT_SETTINGS)

    if raw_default_sensor == DEFAULT_SENSOR_LAST_ACTIVE:
        use_last_active_sensor = True
        default_sensor = None
    else:
        default_sensor = raw_default_sensor

    if default_sensor and default_sensor not in valid_sensor_names:
        _LOGGER.warning(
            "Default sensor '%s' not in config entry %s; falling back to first sensor",
            default_sensor,
            entry.entry_id,
        )
        default_sensor = None

    async_add_entities(
        [
            CustomThermostatEntity(
                hass=hass,
                name=data.get(CONF_NAME, DEFAULT_NAME),
                real_thermostat=data[CONF_THERMOSTAT],
                sensors=sensors,
                default_sensor=default_sensor,
                unique_id=data.get(CONF_UNIQUE_ID) or entry.entry_id,
                physical_sensor_name=physical_sensor_name,
                use_last_active_sensor=use_last_active_sensor,
                cooldown_period=cooldown_period,
                user_min_temp=user_min_temp,
                user_max_temp=user_max_temp,
                single_source_of_truth=single_source_of_truth,
                ignore_thermostat=ignore_thermostat,
                ssot_settings=ssot_settings,
                it_settings=it_settings,
            )
        ]
    )
