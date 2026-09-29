"""
Custom integration for PI temperature and CCA control with Home Assistant.

For more details about this integration, please refer to
https://github.com/helgeklein/ha-pi-thermostat-cca-control
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_loaded_integration

from .config import ConfKeys, get_runtime_configurable_keys, resolve_entry
from .config_flow import OptionsFlowHandler
from .const import (
    DOMAIN,
    HA_OPTIONS,
    INTEGRATION_NAME,
    NUMBER_KEY_CCA_CHARGE_GAIN,
    NUMBER_KEY_CCA_CHARGE_TARGET_SCALE,
    NUMBER_KEY_CCA_DISCHARGE_GAIN,
    NUMBER_KEY_CCA_FORECAST_RESPONSE_STRENGTH,
    NUMBER_KEY_CCA_HOT_DAY_THRESHOLD,
    NUMBER_KEY_CCA_MANUAL_OUTPUT,
    NUMBER_KEY_CCA_OUTPUT_MAX,
    NUMBER_KEY_CCA_OUTPUT_MIN,
    NUMBER_KEY_CCA_OUTPUT_STEP_LIMIT,
    NUMBER_KEY_CCA_THERMAL_STORAGE_PERSISTENCE,
    NUMBER_KEY_CCA_UPDATE_INTERVAL,
    NUMBER_KEY_CCA_WARM_NIGHT_THRESHOLD,
    NUMBER_KEY_TARGET_TEMP,
    SENSOR_KEY_CCA_CHARGE_ESTIMATE,
    SENSOR_KEY_CCA_CHARGE_TARGET,
    SENSOR_KEY_CCA_HEAT_SCORE,
    SENSOR_KEY_CCA_NEXT_UPDATE_IN,
    SENSOR_KEY_CCA_OVERRIDE_ACTIVE,
    SENSOR_KEY_CCA_STATE_STORE,
    SENSOR_KEY_CCA_STATUS,
    SENSOR_KEY_CURRENT_TEMP,
    SENSOR_KEY_DEVIATION,
    SENSOR_KEY_I_TERM,
    SENSOR_KEY_P_TERM,
    SENSOR_KEY_TARGET_TEMP,
    SWITCH_KEY_CCA_MANUAL_OVERRIDE_ENABLED,
    ControlMode,
    TargetTempMode,
)
from .coordinator import DataUpdateCoordinator
from .data import RuntimeData
from .log import Log

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import IntegrationConfigEntry

# List of platforms provided by this integration
PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

CCA_RUNTIME_FORECAST_REFRESH_KEYS: frozenset[str] = frozenset(
    {
        ConfKeys.CCA_CHARGE_TARGET_SCALE.value,
    }
)


#
# async_setup_entry
#
async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntegrationConfigEntry,
) -> bool:
    """Set up the PI Thermostat & CCA Control integration from a config entry.

    This function is called by Home Assistant during:
    - Initial setup of the integration via the UI (after the user completes the config flow)
    - Integration reload (via UI or when config options change)
    - HA restart

    What this function does:
    - Creates the coordinator
    - Merges config + options
    - Stores runtime data on the entry
    - Starts the coordinator
    - Sets up platforms
    - Sets up the reload listener
    """

    logger = Log(entry_id=entry.entry_id)
    logger.info("Starting integration setup")

    try:
        # Create the coordinator
        coordinator = DataUpdateCoordinator(hass, entry)

        # Get configuration from options (all user settings are stored in options)
        merged_config = dict(getattr(entry, HA_OPTIONS, {}) or {})

        # Store the config in the coordinator for comparison during reload
        coordinator._merged_config = merged_config

        # Store shared state
        entry.runtime_data = RuntimeData(
            integration=async_get_loaded_integration(hass, entry.domain),
            coordinator=coordinator,
            config=merged_config,
        )

        await coordinator.async_restore_cca_state()

        # Call each platform's async_setup_entry()
        logger.debug(f"Setting up platforms: {PLATFORMS}")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Remove stale conditional entities from the entity registry.
        _remove_stale_conditional_entities(hass, entry)

        # Trigger initial coordinator refresh after platforms are set up
        # This ensures all entities are registered before the first state update
        logger.debug("Starting initial coordinator refresh")
        await coordinator.async_config_entry_first_refresh()

        # Register the update listener
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    except ConfigEntryNotReady, ConfigEntryAuthFailed:
        raise
    except (OSError, ValueError, TypeError) as err:
        # "Expected" errors: only log an error message
        logger.error(f"Failed to set up {INTEGRATION_NAME} integration: {err}")
        return False
    except Exception as err:
        # "Unexpected" errors: log exception with stack trace
        logger.exception(f"Error during {INTEGRATION_NAME} setup: {err}")
        return False
    else:
        logger.info(f"{INTEGRATION_NAME} integration setup completed")
        return True


#
# _remove_stale_conditional_entities
#
def _remove_stale_conditional_entities(
    hass: HomeAssistant,
    entry: IntegrationConfigEntry,
) -> None:
    """Remove stale PI/CCA conditional entities from the registry."""

    resolved = resolve_entry(entry)
    registry = er.async_get(hass)

    if resolved.control_mode == ControlMode.PI:
        stale_entities: list[tuple[Platform, str]] = [
            (Platform.NUMBER, NUMBER_KEY_CCA_MANUAL_OUTPUT),
            (Platform.NUMBER, NUMBER_KEY_CCA_UPDATE_INTERVAL),
            (Platform.NUMBER, NUMBER_KEY_CCA_HOT_DAY_THRESHOLD),
            (Platform.NUMBER, NUMBER_KEY_CCA_WARM_NIGHT_THRESHOLD),
            (Platform.NUMBER, NUMBER_KEY_CCA_OUTPUT_MIN),
            (Platform.NUMBER, NUMBER_KEY_CCA_OUTPUT_MAX),
            (Platform.NUMBER, NUMBER_KEY_CCA_CHARGE_GAIN),
            (Platform.NUMBER, NUMBER_KEY_CCA_DISCHARGE_GAIN),
            (Platform.NUMBER, NUMBER_KEY_CCA_FORECAST_RESPONSE_STRENGTH),
            (Platform.NUMBER, NUMBER_KEY_CCA_THERMAL_STORAGE_PERSISTENCE),
            (Platform.NUMBER, NUMBER_KEY_CCA_OUTPUT_STEP_LIMIT),
            (Platform.NUMBER, NUMBER_KEY_CCA_CHARGE_TARGET_SCALE),
            (Platform.SENSOR, SENSOR_KEY_CCA_HEAT_SCORE),
            (Platform.SENSOR, SENSOR_KEY_CCA_CHARGE_ESTIMATE),
            (Platform.SENSOR, SENSOR_KEY_CCA_CHARGE_TARGET),
            (Platform.SENSOR, SENSOR_KEY_CCA_OVERRIDE_ACTIVE),
            (Platform.SENSOR, SENSOR_KEY_CCA_STATUS),
            (Platform.SENSOR, SENSOR_KEY_CCA_NEXT_UPDATE_IN),
            (Platform.SENSOR, SENSOR_KEY_CCA_STATE_STORE),
            (Platform.SWITCH, SWITCH_KEY_CCA_MANUAL_OVERRIDE_ENABLED),
        ]
    else:
        stale_entities = [
            (Platform.NUMBER, NUMBER_KEY_TARGET_TEMP),
            (Platform.NUMBER, "proportional_band"),
            (Platform.NUMBER, "integral_time"),
            (Platform.NUMBER, "output_min"),
            (Platform.NUMBER, "output_max"),
            (Platform.NUMBER, "update_interval"),
            (Platform.SENSOR, SENSOR_KEY_DEVIATION),
            (Platform.SENSOR, SENSOR_KEY_CURRENT_TEMP),
            (Platform.SENSOR, SENSOR_KEY_TARGET_TEMP),
            (Platform.SENSOR, SENSOR_KEY_P_TERM),
            (Platform.SENSOR, SENSOR_KEY_I_TERM),
            (Platform.SENSOR, SENSOR_KEY_CCA_STATE_STORE),
            (Platform.NUMBER, NUMBER_KEY_CCA_CHARGE_GAIN),
            (Platform.NUMBER, NUMBER_KEY_CCA_DISCHARGE_GAIN),
        ]

    if resolved.control_mode == ControlMode.PI:
        stale_platform = Platform.SENSOR if resolved.target_temp_mode == TargetTempMode.INTERNAL else Platform.NUMBER
        stale_entities.append((stale_platform, NUMBER_KEY_TARGET_TEMP))
    else:
        stale_entities.append((Platform.SENSOR, SENSOR_KEY_TARGET_TEMP))

    for platform, key in stale_entities:
        unique_id = f"{entry.entry_id}_{key}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)


#
# async_get_options_flow
#
async def async_get_options_flow(entry: IntegrationConfigEntry) -> OptionsFlowHandler:
    """Return the options flow for this handler.

    This function is called by Home Assistant when:
    - The user clicks the gear icon to bring up the integration's options dialog.
    """

    return OptionsFlowHandler(entry)


#
# async_unload_entry
#
async def async_unload_entry(
    hass: HomeAssistant,
    entry: IntegrationConfigEntry,
) -> bool:
    """Handle removal of an entry."""

    logger = Log(entry_id=entry.entry_id)
    logger.info(f"Unloading {INTEGRATION_NAME} integration")

    try:
        result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        return result
    except (OSError, ValueError, TypeError) as err:
        # "Expected" errors: only log an error message
        logger.error(f"Error unloading {INTEGRATION_NAME} integration: {err}")
        return False
    except Exception as err:
        # "Unexpected" errors: log exception with stack trace
        logger.exception(f"Error unloading {INTEGRATION_NAME} integration: {err}")
        return False


#
# async_reload_entry
#
async def async_reload_entry(
    hass: HomeAssistant,
    entry: IntegrationConfigEntry,
) -> None:
    """Reload config entry or just refresh coordinator based on what changed.

    For runtime options that have corresponding entities (switches, numbers),
    we only need to refresh the coordinator. For structural changes, we need a full reload.
    """

    logger = Log(entry_id=entry.entry_id)

    # These keys can be changed at runtime via their corresponding entities
    # without requiring a full reload. The list is centrally defined in config.py
    # based on the runtime_configurable flag in CONF_SPECS.
    runtime_configurable_keys = get_runtime_configurable_keys()

    if hasattr(entry, "runtime_data") and entry.runtime_data:
        coordinator = entry.runtime_data.coordinator

        # Get the old configuration that the coordinator was using
        old_config = coordinator._merged_config

        # Get the new configuration from the updated entry (all settings are in options)
        new_config = dict(getattr(entry, HA_OPTIONS, {}) or {})

        # Determine which keys have actually changed
        changed_keys = {key for key in set(old_config.keys()) | set(new_config.keys()) if old_config.get(key) != new_config.get(key)}
        changes = ", ".join(f"{key}={new_config.get(key)}" for key in sorted(changed_keys))

        # If the only changes are to runtime-configurable keys, just refresh
        if changed_keys and changed_keys.issubset(runtime_configurable_keys):
            logger.info(f"Runtime settings change detected ({changes}), refreshing coordinator")

            # Update the stored config with new values
            coordinator._merged_config = new_config
            entry.runtime_data.config = new_config

            # Trigger a coordinator refresh to apply the changes. In CCA mode,
            # runtime changes must not consume the next scheduled control step.
            if resolve_entry(entry).control_mode == ControlMode.CCA:
                await coordinator.async_request_cca_runtime_recompute(
                    refresh_forecast=bool(changed_keys & CCA_RUNTIME_FORECAST_REFRESH_KEYS)
                )
            else:
                await coordinator.async_request_refresh()
            return

    # For all other changes (structural, new keys, etc.), do a full reload
    logger.info(f"Reloading {INTEGRATION_NAME} integration")
    await hass.config_entries.async_reload(entry.entry_id)


# Re-export common package-level symbols for convenience imports in tooling/tests
__all__ = [
    "DOMAIN",
    "PLATFORMS",
    "async_setup_entry",
    "async_unload_entry",
    "async_reload_entry",
]
