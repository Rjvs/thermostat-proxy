# Thermostat Proxy (Home Assistant)

A Home Assistant custom integration that lets you expose a virtual `climate` entity which mirrors a real thermostat but lets you pick any temperature sensor to act as the “current temperature”. When you change the virtual target temperature, the integration calculates the difference between the selected sensor and the requested set point, then offsets the real thermostat so it behaves as if it were reading the chosen sensor.

## Features

- Wraps an existing `climate` entity; copies all of its attributes for dashboards/automations.
- Supports any number of temperature sensors. Each named sensor becomes a `preset_mode`, so changing the preset swaps the active sensor.
- Falls back to the real thermostat’s `current_temperature` whenever the selected sensor is unknown or unavailable.
- `climate.set_temperature` service adjusts the linked thermostat by the delta between the selected sensor reading and your requested temperature.
- Exposes helper attributes: active sensor, sensor entity id, real current temperature, and the last real target temperature.
- Remembers the previously selected sensor/target temperature across restarts and surfaces an `unavailable_entities` attribute so you can monitor unhealthy dependencies.
- Always adds a built-in `Physical Thermostat` preset that points back to the wrapped thermostat’s own temperature reading so you can revert or set it as the default sensor.

## Installation

1. Copy the `custom_components/thermostat_proxy` directory into your Home Assistant `config/custom_components` folder.
2. Restart Home Assistant.
3. In Home Assistant, go to **Settings → Devices & Services → Add Integration → Thermostat Proxy** and walk through the wizard: pick the physical thermostat, add and name your temperature sensors (each name becomes a preset), and optionally choose which sensor (including the automatically provided “Physical Thermostat” option) should be active by default. You can revisit the integration’s **Configure** button later to change the default sensor without re-creating the entry.

## YAML Configuration

If you prefer YAML, the platform is still available. Add it to the `climate` section of `configuration.yaml` (or a package). To use the built-in preset as your default, set `default_sensor: Physical Thermostat`:

```yaml
climate:
  - platform: thermostat_proxy
    name: Living Room Proxy
    thermostat: climate.living_room_physical
    default_sensor: Kitchen
    sensors:
      - name: Kitchen
        entity_id: sensor.kitchen_temperature
      - name: Bedroom
        entity_id: sensor.bedroom_temperature
      - name: Office
        entity_id: sensor.office_temperature
```

### Options

| Option | Required | Description |
| --- | --- | --- |
| `thermostat` | ✅ | The entity ID of the real thermostat to mirror. |
| `sensors` | ✅ | List of sensor objects with `name` (used as preset) and `entity_id`. |
| `name` | | Friendly name for the new climate entity (`Thermostat Proxy` by default). |
| `default_sensor` | | Name of the sensor to select on startup (defaults to the first item). |

> UI-based setups automatically derive a `unique_id` from the name you choose. If you configure the integration via YAML you can still include `unique_id` manually, but it is optional.

## How It Works

- `current_temperature` reflects the selected sensor. If its state is `unknown`/`unavailable`, the entity reports the real thermostat’s own temperature.
- `preset_modes` is populated with the configured sensor names. Calling `climate.set_preset_mode` switches the sensor.
- When you call `climate.set_temperature` on the custom entity, it calculates `delta = requested_temp - displayed_current_temp` and then sets the real thermostat to `real_current_temp + delta`. A two-degree increase relative to the virtual sensor becomes a two-degree increase on the physical thermostat, for example.
- Requested targets are clamped to the physical thermostat’s `min_temp`, `max_temp`, and `target_temp_step` so automations can’t push the hardware outside its supported range.
- All attributes from the physical thermostat are forwarded as extra attributes, alongside:
  - `active_sensor`
  - `active_sensor_entity_id`
  - `real_current_temperature`
  - `real_target_temperature`
  - `sensor_options`
  - `unavailable_entities`

## Automations / Service Examples

Switch the active sensor (preset):

```yaml
service: climate.set_preset_mode
target:
  entity_id: climate.living_room_proxy
data:
  preset_mode: Kitchen
```

Set a sensor-based target temperature:

```yaml
service: climate.set_temperature
target:
  entity_id: climate.living_room_proxy
data:
  temperature: 73
```

The integration will take the currently selected sensor’s temperature, compare it to the requested value, and offset the real thermostat accordingly.

## Limitations / Notes

- The component assumes a single target temperature (heat, cool, or auto with a shared set point). Dual set points (`target_temp_low` / `target_temp_high`) are not yet supported.
- Because Home Assistant does not expose the real thermostat’s precision/step directly, changes to `current_temperature` or the linked thermostat may momentarily desync the displayed target temperature if another integration changes the physical thermostat. The entity exposes the real target temperature as an attribute so you can reconcile differences.
- Networked thermostats can take a moment to acknowledge new targets; the custom entity updates immediately after the real thermostat reports its new state.
- **Whole-degree sensors will appear “off by one.”** The custom entity only knows the rounded value, so a sensor that reports 67, 68, etc., forces the integration to treat every change as a full degree. In practice this means the virtual thermometer may say “1° below target” while the real thermostat has already closed the gap (because it can see its own 0.5° increments). If you want tighter alignment, pick a temperature sensor that exposes tenths, wrap the existing sensor with a `filter`/average entity, or intentionally bias the real thermostat a little higher via automations.
- **Virtual target may “self-adjust” after the physical thermostat reports an update.** When the integration notices the real thermostat’s target or current temp changed outside of Home Assistant (manual knob, manufacturer cloud), it recomputes the virtual setpoint as `sensor + (real_target - real_current)` so the two stay in sync. With coarse sensors this can look like the HA target jumps from `68` to `67.5` on its own—that simply reflects the real thermostat only needing a half-degree boost.
