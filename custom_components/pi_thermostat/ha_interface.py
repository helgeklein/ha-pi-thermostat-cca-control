"""Home Assistant API interface layer.

This module provides a clean abstraction layer over Home Assistant's APIs,
encapsulating all direct interactions with the HA core system for the
PI Thermostat & CCA Control integration.

All entity state reads and service calls go through this class so that the
coordinator and other modules remain decoupled from HA implementation details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
)
from homeassistant.components.weather import SERVICE_GET_FORECASTS
from homeassistant.components.weather.const import DOMAIN as WEATHER_DOMAIN
from homeassistant.components.weather.const import WeatherEntityFeature
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError

from .log import Log

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# States considered unavailable
_UNAVAILABLE_STATES: frozenset[str] = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


#
# PiThermostatHAError
#
class PiThermostatHAError(Exception):
    """Base class for HA interface errors."""


#
# EntityUnavailableError
#
class EntityUnavailableError(PiThermostatHAError):
    """Entity is unavailable or in an unknown state."""

    #
    # __init__
    #
    def __init__(self, entity_id: str) -> None:
        """Initialize with the entity ID that is unavailable."""

        super().__init__(f"Entity '{entity_id}' is unavailable")
        self.entity_id = entity_id


#
# InvalidSensorReadingError
#
class InvalidSensorReadingError(PiThermostatHAError):
    """Sensor reading could not be converted to a numeric value."""

    #
    # __init__
    #
    def __init__(self, entity_id: str, value: str) -> None:
        """Initialize with entity ID and the invalid value."""

        super().__init__(f"Invalid reading from '{entity_id}': {value}")
        self.entity_id = entity_id
        self.value = value


#
# ServiceCallError
#
class ServiceCallError(PiThermostatHAError):
    """A service call to Home Assistant failed."""

    #
    # __init__
    #
    def __init__(self, service: str, entity_id: str, error: str) -> None:
        """Initialize with service name, entity ID, and error details."""

        super().__init__(f"Failed to call {service} for {entity_id}: {error}")
        self.service = service
        self.entity_id = entity_id
        self.error = error


# ---------------------------------------------------------------------------
# HomeAssistantInterface
# ---------------------------------------------------------------------------


#
# HomeAssistantInterface
#
class HomeAssistantInterface:
    """Encapsulates all Home Assistant API interactions.

    Provides typed accessor methods for reading entity states/attributes
    and calling services, keeping the coordinator decoupled from HA internals.
    """

    #
    # __init__
    #
    def __init__(self, hass: HomeAssistant, logger: Log) -> None:
        """Initialize the HA interface.

        Args:
            hass: Home Assistant instance.
            logger: Instance-specific logger with entry_id prefix.
        """

        self._hass = hass
        self._logger = logger

    # ------------------------------------------------------------------
    # State helpers (private)
    # ------------------------------------------------------------------

    #
    # _get_state_obj
    #
    def _get_state_obj(self, entity_id: str) -> Any | None:
        """Return the state object for *entity_id*, or ``None`` if missing."""

        return self._hass.states.get(entity_id)

    #
    # _get_float_state
    #
    def _get_float_state(self, entity_id: str) -> float | None:
        """Read the main state of *entity_id* as a float.

        Returns:
            The numeric value, or ``None`` if the entity is unavailable or the
            state cannot be converted.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None or state_obj.state in _UNAVAILABLE_STATES:
            return None
        try:
            return float(state_obj.state)
        except ValueError, TypeError:
            self._logger.warning(
                "Cannot convert state of %s to float: %s",
                entity_id,
                state_obj.state,
            )
            return None

    #
    # _get_float_attribute
    #
    def _get_float_attribute(self, entity_id: str, attribute: str) -> float | None:
        """Read a numeric attribute from *entity_id*.

        Returns:
            The numeric value, or ``None`` if the entity/attribute is missing
            or cannot be converted.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None or state_obj.state in _UNAVAILABLE_STATES:
            return None
        value = state_obj.attributes.get(attribute)
        if value is None:
            return None
        try:
            return float(value)
        except ValueError, TypeError:
            self._logger.warning(
                "Cannot convert attribute %s of %s to float: %s",
                attribute,
                entity_id,
                value,
            )
            return None

    #
    # _get_str_attribute
    #
    def _get_str_attribute(self, entity_id: str, attribute: str) -> str | None:
        """Read a string attribute from *entity_id*.

        Returns:
            The string value, or ``None`` if the entity/attribute is missing
            or the entity is unavailable.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None or state_obj.state in _UNAVAILABLE_STATES:
            return None
        value = state_obj.attributes.get(attribute)
        return str(value) if value is not None else None

    # ------------------------------------------------------------------
    # Public API — temperature readings
    # ------------------------------------------------------------------

    #
    # get_temperature
    #
    def get_temperature(self, entity_id: str) -> float | None:
        """Read the current temperature from a sensor entity.

        Args:
            entity_id: Entity ID of a sensor with device_class temperature.

        Returns:
            Temperature as float, or ``None`` if unavailable.
        """

        return self._get_float_state(entity_id)

    #
    # get_climate_current_temperature
    #
    def get_climate_current_temperature(self, entity_id: str) -> float | None:
        """Read ``current_temperature`` from a climate entity.

        Args:
            entity_id: Entity ID of a climate entity.

        Returns:
            Current temperature as float, or ``None`` if unavailable.
        """

        return self._get_float_attribute(entity_id, ATTR_CURRENT_TEMPERATURE)

    #
    # get_target_temperature
    #
    def get_target_temperature(self, entity_id: str) -> float | None:
        """Read the target temperature from an external entity (number, input_number).

        Args:
            entity_id: Entity ID of the target temperature entity.

        Returns:
            Target temperature as float, or ``None`` if unavailable.
        """

        return self._get_float_state(entity_id)

    #
    # get_climate_target_temperature
    #
    def get_climate_target_temperature(self, entity_id: str) -> float | None:
        """Read the ``temperature`` (setpoint) attribute from a climate entity.

        Args:
            entity_id: Entity ID of a climate entity.

        Returns:
            Target temperature as float, or ``None`` if unavailable.
        """

        return self._get_float_attribute(entity_id, ATTR_TEMPERATURE)

    # ------------------------------------------------------------------
    # Public API — climate entity attributes
    # ------------------------------------------------------------------

    #
    # get_climate_hvac_action
    #
    def get_climate_hvac_action(self, entity_id: str) -> str | None:
        """Read the ``hvac_action`` attribute from a climate entity.

        Used in ``heat_cool`` operating mode to determine if the system is
        currently heating or cooling.

        Args:
            entity_id: Entity ID of a climate entity.

        Returns:
            HVAC action string (e.g. "heating", "cooling", "idle"), or
            ``None`` if unavailable.
        """

        return self._get_str_attribute(entity_id, ATTR_HVAC_ACTION)

    #
    # get_climate_hvac_mode
    #
    def get_climate_hvac_mode(self, entity_id: str) -> str | None:
        """Read the ``hvac_mode`` (main state) from a climate entity.

        Used for auto-disable: if the climate entity's mode is "off", the
        controller output should be set to 0%.

        Args:
            entity_id: Entity ID of a climate entity.

        Returns:
            HVAC mode string (e.g. "off", "heat", "cool", "heat_cool"),
            or ``None`` if unavailable.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None or state_obj.state in _UNAVAILABLE_STATES:
            return None
        return str(state_obj.state)

    # ------------------------------------------------------------------
    # Public API — weather forecast helpers
    # ------------------------------------------------------------------

    #
    # async_validate_daily_forecast_support
    #
    async def async_validate_daily_forecast_support(self, entity_id: str) -> None:
        """Validate that a weather entity exists and supports daily forecasts.

        Args:
            entity_id: Weather entity to validate.

        Raises:
            HomeAssistantError: If the entity is missing or lacks daily forecasts.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None:
            raise HomeAssistantError(f"Weather entity '{entity_id}' was not found")

        supported_features = int(state_obj.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
        if (supported_features & WeatherEntityFeature.FORECAST_DAILY) == 0:
            raise HomeAssistantError(f"Weather entity '{entity_id}' does not support daily forecasts")

    #
    # async_get_daily_forecasts
    #
    async def async_get_daily_forecasts(self, entity_id: str) -> list[dict[str, Any]]:
        """Return daily forecasts for a weather entity.

        Args:
            entity_id: Weather entity to query.

        Returns:
            Forecast list returned by Home Assistant.

        Raises:
            HomeAssistantError: If the forecast request fails.
        """

        await self.async_validate_daily_forecast_support(entity_id)

        response = await self._hass.services.async_call(
            WEATHER_DOMAIN,
            SERVICE_GET_FORECASTS,
            {"type": "daily"},
            blocking=True,
            target={"entity_id": entity_id},
            return_response=True,
        )

        if not isinstance(response, dict):
            raise HomeAssistantError(f"Daily forecast request for '{entity_id}' returned no response")

        entity_response = response.get(entity_id, response)
        if not isinstance(entity_response, dict):
            raise HomeAssistantError(f"Daily forecast request for '{entity_id}' returned an invalid response")

        forecast = entity_response.get("forecast", [])
        if not isinstance(forecast, list):
            raise HomeAssistantError(f"Daily forecast request for '{entity_id}' returned an invalid forecast payload")

        return [item for item in forecast if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # Public API — availability
    # ------------------------------------------------------------------

    #
    # is_entity_available
    #
    def is_entity_available(self, entity_id: str) -> bool:
        """Check whether an entity is available (state is not unavailable/unknown).

        Args:
            entity_id: Entity ID to check.

        Returns:
            ``True`` if the entity exists and its state is not in an
            unavailable/unknown state.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None:
            return False
        return state_obj.state not in _UNAVAILABLE_STATES

    #
    # get_entity_on_state
    #
    def get_entity_on_state(self, entity_id: str) -> bool | None:
        """Interpret a generic entity state while preserving unreadable states.

        Returns:
            ``True`` for explicit on-states, ``False`` for explicit off-states,
            and ``None`` if the entity is missing, unavailable, or cannot be
            interpreted as a boolean signal.
        """

        state_obj = self._get_state_obj(entity_id)
        if state_obj is None or state_obj.state in _UNAVAILABLE_STATES:
            return None

        normalized = str(state_obj.state).strip().lower()
        if normalized in {"on", "true"}:
            return True
        if normalized in {"off", "false"}:
            return False

        try:
            return float(normalized) != 0.0
        except ValueError:
            return None

    #
    # is_entity_on
    #
    def is_entity_on(self, entity_id: str) -> bool:
        """Interpret a generic entity state as an enabled/active boolean."""

        return self.get_entity_on_state(entity_id) is True
