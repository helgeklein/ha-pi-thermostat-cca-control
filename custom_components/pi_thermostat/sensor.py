"""Sensor platform for pi_thermostat.

Provides read-only sensors exposing the PI controller's internal state:

- **output**: PI output percentage (0-100 %).
- **deviation**: Control deviation (target - current temperature).
- **current_mode**: Current controller mode (heating, cooling, off).
- **current_temp**: Current temperature reading.
- **target_temp**: Target temperature (read-only; only when target_temp_mode is not internal).
- **p_term**: Proportional component of the output.
- **i_term**: Integral component (uses ``RestoreEntity`` for persistence across restarts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.restore_state import RestoreEntity

from .config import resolve_entry
from .const import (
    SENSOR_KEY_CCA_CHARGE_ESTIMATE,
    SENSOR_KEY_CCA_CHARGE_TARGET,
    SENSOR_KEY_CCA_HEAT_SCORE,
    SENSOR_KEY_CCA_NEXT_UPDATE_IN,
    SENSOR_KEY_CCA_OVERRIDE_ACTIVE,
    SENSOR_KEY_CCA_STATUS,
    SENSOR_KEY_CURRENT_MODE,
    SENSOR_KEY_CURRENT_TEMP,
    SENSOR_KEY_DEVIATION,
    SENSOR_KEY_I_TERM,
    SENSOR_KEY_OUTPUT,
    SENSOR_KEY_P_TERM,
    SENSOR_KEY_TARGET_TEMP,
    ControlMode,
    TargetTempMode,
)
from .entity import IntegrationEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import DataUpdateCoordinator
    from .data import IntegrationConfigEntry


# ---------------------------------------------------------------------------
# Sensor descriptions
# ---------------------------------------------------------------------------

SENSOR_OUTPUT = SensorEntityDescription(
    key=SENSOR_KEY_OUTPUT,
    translation_key=SENSOR_KEY_OUTPUT,
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:gauge",
    suggested_display_precision=1,
)

SENSOR_DEVIATION = SensorEntityDescription(
    key=SENSOR_KEY_DEVIATION,
    translation_key=SENSOR_KEY_DEVIATION,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=2,
)

SENSOR_CURRENT_MODE = SensorEntityDescription(
    key=SENSOR_KEY_CURRENT_MODE,
    translation_key=SENSOR_KEY_CURRENT_MODE,
    device_class=SensorDeviceClass.ENUM,
    options=["heating", "cooling", "off"],
    icon="mdi:hvac",
)

SENSOR_CURRENT_TEMP = SensorEntityDescription(
    key=SENSOR_KEY_CURRENT_TEMP,
    translation_key=SENSOR_KEY_CURRENT_TEMP,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_TARGET_TEMP = SensorEntityDescription(
    key=SENSOR_KEY_TARGET_TEMP,
    translation_key=SENSOR_KEY_TARGET_TEMP,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_P_TERM = SensorEntityDescription(
    key=SENSOR_KEY_P_TERM,
    translation_key=SENSOR_KEY_P_TERM,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.DIAGNOSTIC,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_I_TERM = SensorEntityDescription(
    key=SENSOR_KEY_I_TERM,
    translation_key=SENSOR_KEY_I_TERM,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.DIAGNOSTIC,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_CCA_HEAT_SCORE = SensorEntityDescription(
    key=SENSOR_KEY_CCA_HEAT_SCORE,
    translation_key=SENSOR_KEY_CCA_HEAT_SCORE,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.DIAGNOSTIC,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_CCA_CHARGE_ESTIMATE = SensorEntityDescription(
    key=SENSOR_KEY_CCA_CHARGE_ESTIMATE,
    translation_key=SENSOR_KEY_CCA_CHARGE_ESTIMATE,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.DIAGNOSTIC,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_CCA_CHARGE_TARGET = SensorEntityDescription(
    key=SENSOR_KEY_CCA_CHARGE_TARGET,
    translation_key=SENSOR_KEY_CCA_CHARGE_TARGET,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.DIAGNOSTIC,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSOR_CCA_OVERRIDE_ACTIVE = SensorEntityDescription(
    key=SENSOR_KEY_CCA_OVERRIDE_ACTIVE,
    translation_key=SENSOR_KEY_CCA_OVERRIDE_ACTIVE,
    device_class=SensorDeviceClass.ENUM,
    options=["off", "on"],
    entity_category=EntityCategory.DIAGNOSTIC,
)

SENSOR_CCA_STATUS = SensorEntityDescription(
    key=SENSOR_KEY_CCA_STATUS,
    translation_key=SENSOR_KEY_CCA_STATUS,
    device_class=SensorDeviceClass.ENUM,
    options=[
        "idle",
        "inactive",
        "forecast_hold",
        "forecast_unavailable",
        "active",
        "manual_override",
    ],
    entity_category=EntityCategory.DIAGNOSTIC,
)

SENSOR_CCA_NEXT_UPDATE_IN = SensorEntityDescription(
    key=SENSOR_KEY_CCA_NEXT_UPDATE_IN,
    translation_key=SENSOR_KEY_CCA_NEXT_UPDATE_IN,
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    entity_category=EntityCategory.DIAGNOSTIC,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


#
# async_setup_entry
#
async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: IntegrationConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create all sensor entities for a config entry."""

    coordinator = entry.runtime_data.coordinator
    resolved = resolve_entry(entry)

    entities: list[IntegrationSensor | ITermSensor] = [
        IntegrationSensor(coordinator, SENSOR_OUTPUT),
        IntegrationSensor(coordinator, SENSOR_CURRENT_MODE),
    ]

    if resolved.control_mode == ControlMode.PI:
        entities.extend(
            [
                IntegrationSensor(coordinator, SENSOR_DEVIATION),
                IntegrationSensor(coordinator, SENSOR_CURRENT_TEMP),
                IntegrationSensor(coordinator, SENSOR_P_TERM),
                ITermSensor(coordinator),
            ]
        )

        if resolved.target_temp_mode != TargetTempMode.INTERNAL:
            entities.append(IntegrationSensor(coordinator, SENSOR_TARGET_TEMP))
    else:
        entities.extend(
            [
                IntegrationSensor(coordinator, SENSOR_CCA_HEAT_SCORE),
                IntegrationSensor(coordinator, SENSOR_CCA_CHARGE_ESTIMATE),
                IntegrationSensor(coordinator, SENSOR_CCA_CHARGE_TARGET),
                IntegrationSensor(coordinator, SENSOR_CCA_OVERRIDE_ACTIVE),
                IntegrationSensor(coordinator, SENSOR_CCA_STATUS),
                IntegrationSensor(coordinator, SENSOR_CCA_NEXT_UPDATE_IN),
            ]
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Sensor entity classes
# ---------------------------------------------------------------------------


#
# IntegrationSensor
#
class IntegrationSensor(IntegrationEntity, SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Generic read-only sensor backed by ``CoordinatorData``.

    The sensor's value is read from the attribute of ``CoordinatorData``
    whose name matches the entity description's *key*.
    """

    #
    # __init__
    #
    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor.

        Args:
            coordinator: The coordinator providing data for this sensor.
            entity_description: Descriptor defining the entity's HA properties.
        """

        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"

    #
    # native_value
    #
    @property
    def native_value(self) -> str | float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the current sensor value from coordinator data."""

        if self.coordinator.data is None:
            return None
        return getattr(self.coordinator.data, self.entity_description.key, None)


# ---------------------------------------------------------------------------
# I-term sensor with RestoreEntity for integral persistence
# ---------------------------------------------------------------------------


#
# ITermSensor
#
class ITermSensor(IntegrationEntity, RestoreEntity, SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Integral-term sensor with state persistence across restarts.

    On startup, the integral term is initialized based on the configured
    startup mode:

    - **last**: Restore from HA's state machine. If no persisted value is
      available (e.g., first startup), fall back to the user-provided
      startup value (default: 0).
    - **fixed**: Always use the user-provided startup value.
    - **zero**: Always start at 0%.
    """

    #
    # __init__
    #
    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the I-term sensor.

        Args:
            coordinator: The coordinator providing data for this sensor.
        """

        super().__init__(coordinator)
        self.entity_description = SENSOR_I_TERM
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{SENSOR_I_TERM.key}"

    #
    # async_added_to_hass
    #
    async def async_added_to_hass(self) -> None:
        """Initialize the integral term based on the configured startup mode.

        Steps:
        1. Call the parent implementation to register coordinator listeners.
        2. Read the startup mode from the resolved configuration.
        3. Depending on mode:
           - **zero**: Do nothing (PI controller defaults to 0).
           - **fixed**: Use the configured startup value.
           - **last**: Attempt to restore from persisted state; fall back to
             the configured startup value if unavailable.
        """

        await super().async_added_to_hass()

        from .config import resolve_entry
        from .const import ITermStartupMode

        resolved = resolve_entry(self.coordinator.config_entry)
        mode = resolved.iterm_startup_mode
        startup_value = resolved.iterm_startup_value

        if mode == ITermStartupMode.ZERO:
            # PI controller starts at 0 by default — nothing to do
            return

        if mode == ITermStartupMode.FIXED:
            self.coordinator.restore_integral_term(startup_value)
            return

        # mode == ITermStartupMode.LAST: restore from persisted state
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "", "unknown", "unavailable"):
            try:
                restored_value = float(last_state.state)
                self.coordinator.restore_integral_term(restored_value)
                return
            except ValueError, TypeError:
                pass

        # No valid persisted state — fall back to startup value
        if startup_value != 0.0:
            self.coordinator.restore_integral_term(startup_value)

    #
    # native_value
    #
    @property
    def native_value(self) -> float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the current integral term from coordinator data."""

        if self.coordinator.data is None:
            return None
        return self.coordinator.data.i_term
