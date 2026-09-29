"""PI Thermostat & CCA Control coordinator - update loop.

Reads temperatures, delegates to the PI controller, and returns
``CoordinatorData`` consumed by all entities.

The ``_async_update_data`` cycle runs on every update interval:

 1. Resolve configuration from config entry options.
 1b. Check the enabled flag — CCA shuts down to 0, PI pauses and preserves the last state.
 2. Auto-disable when the climate entity's HVAC mode is "off".
 3. Determine heating / cooling direction (fixed or from climate entity).
 4. Read the current temperature (sensor or climate entity).
 5. Determine the target temperature (internal, external, or climate).
 6. Handle sensor faults (shutdown immediately or hold then shutdown).
 7. Apply any runtime tuning changes to the PI controller.
 8. Run the PI controller to get output, deviation, P-term, and I-term.
 9. Write the output value to the configured output entity (optional).
10. Return ``CoordinatorData`` for consumption by all entities.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator as BaseCoordinator,
)
from homeassistant.helpers.update_coordinator import (
    UpdateFailed,
)

from .cca_controller import CCAControllerStrategy, CCAState
from .config import ResolvedConfig, resolve_entry
from .const import (
    CCA_COORDINATOR_POLL_INTERVAL_SECONDS,
    CCA_STATE_STORAGE_KEY_PREFIX,
    CCA_STATE_STORAGE_VERSION,
    DOMAIN,
    HA_OPTIONS,
    SENSOR_FAULT_GRACE_PERIOD_SECONDS,
    ControlMode,
    OperatingMode,
    SensorFaultMode,
    TargetTempMode,
)
from .data import CoordinatorData
from .ha_interface import HomeAssistantInterface
from .log import Log
from .pi_controller import PIController

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import IntegrationConfigEntry


CCA_VALID_STATUSES: frozenset[str] = frozenset(
    {
        "idle",
        "inactive",
        "forecast_hold",
        "forecast_unavailable",
        "active",
        "manual_override",
    }
)


# ---------------------------------------------------------------------------
# DataUpdateCoordinator
# ---------------------------------------------------------------------------


#
# DataUpdateCoordinator
#
class DataUpdateCoordinator(BaseCoordinator[CoordinatorData]):
    """PI Thermostat & CCA Control update coordinator."""

    config_entry: IntegrationConfigEntry

    #
    # __init__
    #
    def __init__(self, hass: HomeAssistant, config_entry: IntegrationConfigEntry) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance.
            config_entry: The integration's config entry.
        """

        # Instance-specific logger
        self._logger = Log(entry_id=config_entry.entry_id)

        resolved = resolve_entry(config_entry)
        update_interval_seconds = self._initial_update_interval_seconds(resolved)

        super().__init__(
            hass,
            self._logger.underlying_logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval_seconds),
            config_entry=config_entry,
        )
        self.config_entry = config_entry

        # Merged config dict for reload-comparison in __init__.py
        self._merged_config: dict[str, Any] = {}

        # HA abstraction layer
        self._ha = HomeAssistantInterface(hass, self._logger)

        # PI controller — created with initial resolved settings
        self._pi = PIController(
            proportional_band=resolved.proportional_band,
            integral_time_min=resolved.integral_time,
            output_min=resolved.output_min,
            output_max=resolved.output_max,
            sample_time=float(resolved.update_interval),
            setpoint=resolved.target_temp,
            is_cooling=(resolved.operating_mode == OperatingMode.COOL),
        )

        # CCA controller state/strategy
        self._cca = CCAControllerStrategy()
        self._cca_store: Store[dict[str, Any]] = Store(
            hass,
            CCA_STATE_STORAGE_VERSION,
            f"{DOMAIN}.{CCA_STATE_STORAGE_KEY_PREFIX}.{config_entry.entry_id}",
        )

        # Sensor-fault tracking for HOLD mode
        self._fault_cycles: int = 0
        self._last_good_output: float | None = None

        # Last coordinator result — used to preserve state when paused
        self._last_data: CoordinatorData | None = None

        # One-cycle startup grace for a restored active CCA state while the cooling gate is unreadable.
        self._cca_restore_gate_grace = False

        # One-shot flag used to fetch forecasts and recompute the automatic
        # output immediately without consuming the scheduled CCA step.
        self._cca_runtime_forecast_refresh_pending = False

        # Track last-applied tunings to detect changes
        self._last_prop_band: float = resolved.proportional_band
        self._last_int_time: float = resolved.integral_time
        self._last_output_min: float = resolved.output_min
        self._last_output_max: float = resolved.output_max
        self._last_update_interval: int = resolved.update_interval

        self._logger.info(
            "Coordinator initialized: update_interval=%s s, prop_band=%s K, int_time=%s min, mode=%s",
            update_interval_seconds,
            resolved.proportional_band,
            resolved.integral_time,
            resolved.control_mode,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    #
    # restore_integral_term
    #
    def restore_integral_term(self, value: float) -> None:
        """Restore the integral term after a restart (called by the i_term sensor).

        Args:
            value: Previously persisted integral term.
        """

        self._pi.restore_integral_term(value)
        self._logger.info("Restored integral term: %s", value)

    #
    # restore_cca_state
    #
    def restore_cca_state(self, state: CCAState) -> None:
        """Restore the CCA controller state after restart."""

        self._cca.restore_state(state)
        self._cca_restore_gate_grace = state.last_auto_output > 0.0 and state.status != "inactive"
        self._logger.info("Restored CCA state: charge=%s status=%s", state.charge_estimate, state.status)

    #
    # get_cca_state
    #
    def get_cca_state(self) -> CCAState:
        """Return the current CCA state for persistence entities."""

        return self._cca.get_state()

    #
    # async_request_cca_runtime_recompute
    #
    async def async_request_cca_runtime_recompute(self, *, refresh_forecast: bool = False) -> None:
        """Apply runtime CCA option changes.

        Args:
            refresh_forecast: When ``True``, the next CCA coordinator run fetches
                forecasts and recomputes the automatic output immediately without
                advancing the scheduled CCA step.
        """

        if refresh_forecast:
            self._cca_runtime_forecast_refresh_pending = True

        await self.async_request_refresh()

    #
    # async_restore_cca_state
    #
    async def async_restore_cca_state(self) -> None:
        """Restore persisted CCA state from storage when CCA mode is active."""

        resolved = self._resolve()
        if resolved.control_mode != ControlMode.CCA:
            return

        payload = await self._cca_store.async_load()
        if not payload:
            return

        try:
            restored_state = CCAState(
                charge_estimate=float(payload.get("charge_estimate", 0.0)),
                last_auto_output=float(payload.get("last_auto_output", 0.0)),
                last_heat_score=float(payload.get("last_heat_score", 0.0)),
                last_step_timestamp_iso=payload.get("last_step_timestamp_iso") or payload.get("last_update_iso"),
                status=str(payload.get("status", "idle")),
            )
        except TypeError, ValueError:
            self._logger.warning("Invalid persisted CCA state ignored")
            return

        normalized_state = self._normalize_restored_cca_state(restored_state)
        self.restore_cca_state(normalized_state)

        if normalized_state != restored_state:
            self._logger.warning("Normalized persisted CCA state during restore")
            await self._async_save_cca_state(normalized_state)

    #
    # _async_save_cca_state
    #
    async def _async_save_cca_state(self, state: CCAState) -> None:
        """Persist the current CCA state to storage."""

        await self._cca_store.async_save(
            {
                "charge_estimate": state.charge_estimate,
                "last_auto_output": state.last_auto_output,
                "last_heat_score": state.last_heat_score,
                "last_step_timestamp_iso": state.last_step_timestamp_iso,
                "status": state.status,
            }
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    #
    # _resolve
    #
    def _resolve(self) -> ResolvedConfig:
        """Return resolved settings from the config entry options."""

        from .config import resolve

        opts = dict(getattr(self.config_entry, HA_OPTIONS, {}) or {})
        return resolve(opts)

    #
    # _normalize_restored_cca_state
    #
    def _normalize_restored_cca_state(self, state: CCAState) -> CCAState:
        """Return a restored CCA state clamped to valid persisted ranges."""

        last_step_timestamp_iso = state.last_step_timestamp_iso
        parsed_last_step_timestamp = self._parse_cca_last_step_timestamp(last_step_timestamp_iso)
        if last_step_timestamp_iso is not None and parsed_last_step_timestamp is None:
            last_step_timestamp_iso = None
        elif parsed_last_step_timestamp is not None:
            last_step_timestamp_iso = parsed_last_step_timestamp.isoformat()

        status = state.status if state.status in CCA_VALID_STATUSES else "idle"

        return replace(
            state,
            charge_estimate=max(0.0, min(100.0, state.charge_estimate)),
            last_auto_output=max(0.0, min(100.0, state.last_auto_output)),
            last_heat_score=max(0.0, min(100.0, state.last_heat_score)),
            last_step_timestamp_iso=last_step_timestamp_iso,
            status=status,
        )

    #
    # _initial_update_interval_seconds
    #
    @staticmethod
    def _initial_update_interval_seconds(resolved: ResolvedConfig) -> int:
        """Return the coordinator interval for the selected control mode."""

        if resolved.control_mode == ControlMode.CCA:
            return CCA_COORDINATOR_POLL_INTERVAL_SECONDS
        return resolved.update_interval

    #
    # _cca_update_interval
    #
    @staticmethod
    def _cca_update_interval(resolved: ResolvedConfig) -> timedelta:
        """Return the configured elapsed time between automatic CCA control steps."""

        return timedelta(minutes=resolved.cca_update_interval_minutes)

    #
    # _paused_result
    #
    def _paused_result(self) -> CoordinatorData:
        """Return a CoordinatorData that preserves the last state (controller paused).

        When no previous data exists (e.g. first cycle after startup with
        the enabled switch off), returns a safe default with output = 0.
        The output entity is **not** written to, so whatever value the
        external entity already has is preserved.
        """

        if self._last_data is not None:
            return CoordinatorData(
                output=self._last_data.output,
                deviation=self._last_data.deviation,
                current_mode=self._last_data.current_mode,
                p_term=self._last_data.p_term,
                i_term=self._last_data.i_term,
                current_temp=self._last_data.current_temp,
                target_temp=self._last_data.target_temp,
                sensor_available=self._last_data.sensor_available,
                cca_heat_score=self._last_data.cca_heat_score,
                cca_charge_estimate=self._last_data.cca_charge_estimate,
                cca_charge_target=self._last_data.cca_charge_target,
                cca_override_active=self._last_data.cca_override_active,
                cca_status=self._last_data.cca_status,
            )

        return self._unknown_result(current_mode="off")

    #
    # _shutdown_result
    #
    @staticmethod
    def _shutdown_result(
        *,
        current_temp: float | None = None,
        target_temp: float | None = None,
        sensor_available: bool = True,
    ) -> CoordinatorData:
        """Return a CoordinatorData with output = 0 (shutdown / auto-disabled)."""

        return CoordinatorData(
            output=0.0,
            deviation=None,
            current_mode="off",
            p_term=None,
            i_term=None,
            current_temp=current_temp,
            target_temp=target_temp,
            sensor_available=sensor_available,
        )

    #
    # _unknown_result
    #
    @staticmethod
    def _unknown_result(
        *,
        current_temp: float | None = None,
        target_temp: float | None = None,
        sensor_available: bool = True,
        current_mode: str | None = None,
    ) -> CoordinatorData:
        """Return a CoordinatorData with output = None (no known-good value yet).

        Used when the coordinator cannot determine a valid output and should
        not change whatever value entities already have (e.g. their restored
        state after a restart).
        """

        return CoordinatorData(
            output=None,
            deviation=None,
            current_mode=current_mode,
            p_term=None,
            i_term=None,
            current_temp=current_temp,
            target_temp=target_temp,
            sensor_available=sensor_available,
        )

    #
    # _current_mode
    #
    @staticmethod
    def _current_mode(is_cooling: bool) -> str:
        """Return the controller mode string for the resolved direction."""

        if is_cooling:
            return "cooling"
        return "heating"

    #
    # _parse_cca_last_step_timestamp
    #
    @staticmethod
    def _parse_cca_last_step_timestamp(last_step_timestamp_iso: str | None) -> datetime | None:
        """Parse the persisted CCA step timestamp."""

        if not last_step_timestamp_iso:
            return None

        try:
            parsed = datetime.fromisoformat(last_step_timestamp_iso)
        except ValueError:
            return None

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    #
    # _is_cca_update_due
    #
    def _is_cca_update_due(self, resolved: ResolvedConfig) -> bool:
        """Return whether the next automatic CCA control step is due."""

        last_step_timestamp = self._parse_cca_last_step_timestamp(self._cca.get_state().last_step_timestamp_iso)
        if last_step_timestamp is None:
            return True

        update_interval = self._cca_update_interval(resolved)
        return datetime.now(UTC) - last_step_timestamp >= update_interval

    #
    # _cca_next_update_in_minutes
    #
    def _cca_next_update_in_minutes(self, resolved: ResolvedConfig) -> int:
        """Return the remaining minutes until the next automatic CCA control step."""

        last_step_timestamp = self._parse_cca_last_step_timestamp(self._cca.get_state().last_step_timestamp_iso)
        if last_step_timestamp is None:
            return 0

        remaining = self._cca_update_interval(resolved) - (datetime.now(UTC) - last_step_timestamp)
        if remaining <= timedelta(0):
            return 0

        return ceil(remaining.total_seconds() / 60.0)

    #
    # _build_cca_refresh_result
    #
    def _build_cca_refresh_result(self, resolved: ResolvedConfig) -> CoordinatorData:
        """Return the current CCA output without consuming another control step."""

        state = self._normalized_cca_refresh_state(resolved)
        output = state.last_auto_output
        override_active = "off"
        status = state.status

        if resolved.cca_manual_override_enabled:
            output = max(0.0, min(100.0, resolved.cca_manual_output))
            override_active = "on"

        current_mode = "cooling" if output > 0 else "off"
        heat_score = state.last_heat_score if self._last_data is None else self._last_data.cca_heat_score
        charge_target = self._cca.compute_charge_target(state.last_heat_score, resolved)

        return CoordinatorData(
            output=output,
            current_mode=current_mode,
            sensor_available=True,
            cca_heat_score=heat_score,
            cca_charge_estimate=state.charge_estimate,
            cca_charge_target=charge_target,
            cca_override_active=override_active,
            cca_status=status,
            cca_next_update_in=self._cca_next_update_in_minutes(resolved),
        )

    #
    # _normalized_cca_refresh_state
    #
    def _normalized_cca_refresh_state(self, resolved: ResolvedConfig) -> CCAState:
        """Return the cached CCA state normalized for immediate runtime setting changes."""

        state = self._cca.get_state()
        last_auto_output = max(0.0, min(100.0, state.last_auto_output))
        status = state.status

        if resolved.cca_manual_override_enabled:
            status = "manual_override"
        else:
            if last_auto_output > 0.0:
                last_auto_output = max(resolved.cca_output_min, min(resolved.cca_output_max, last_auto_output))

            if status == "manual_override":
                status = "active"

        return replace(
            state,
            last_auto_output=last_auto_output,
            status=status,
        )

    #
    # _async_apply_cca_refresh_state
    #
    async def _async_apply_cca_refresh_state(self, resolved: ResolvedConfig) -> None:
        """Persist any cached CCA state normalization caused by a runtime refresh."""

        previous_state = self._cca.get_state()
        normalized_state = self._normalized_cca_refresh_state(resolved)
        if normalized_state == previous_state:
            return

        self._cca.restore_state(normalized_state)
        await self._async_save_cca_state(normalized_state)

    #
    # _should_preserve_unreadable_cca_gate_state
    #
    def _consume_cca_restore_gate_grace(self) -> bool:
        """Consume the one-cycle startup grace for a restored active CCA state."""

        if not self._cca_restore_gate_grace:
            return False

        self._cca_restore_gate_grace = False
        return True

    #
    # _should_recompute_cca_on_gate_enable
    #
    def _should_recompute_cca_on_gate_enable(self) -> bool:
        """Return whether a readable enabled gate should force immediate recovery from inactivity."""

        return self._cca.get_state().status == "inactive"

    #
    # _read_current_temp
    #
    def _read_current_temp(self, resolved: ResolvedConfig) -> float | None:
        """Read the current temperature from the configured source.

        Priority:
        1. Dedicated temperature sensor entity (temp_sensor).
        2. Climate entity's current_temperature attribute.

        Returns:
            Temperature as float, or ``None`` if unavailable.
        """

        if resolved.temp_sensor:
            return self._ha.get_temperature(resolved.temp_sensor)

        if resolved.climate_entity:
            return self._ha.get_climate_current_temperature(resolved.climate_entity)

        return None

    #
    # _read_target_temp
    #
    def _read_target_temp(self, resolved: ResolvedConfig) -> float | None:
        """Read the target temperature from the configured source.

        Returns:
            Target temperature, or ``None`` if unavailable from an external source.
        """

        from .const import TargetTempMode

        mode = resolved.target_temp_mode

        if mode == TargetTempMode.INTERNAL:
            return resolved.target_temp

        if mode == TargetTempMode.EXTERNAL and resolved.target_temp_entity:
            return self._ha.get_target_temperature(resolved.target_temp_entity)

        if mode == TargetTempMode.CLIMATE and resolved.climate_entity:
            return self._ha.get_climate_target_temperature(resolved.climate_entity)

        # Fallback — no valid source
        return None

    #
    # _apply_tuning_changes
    #
    def _apply_tuning_changes(self, resolved: ResolvedConfig) -> None:
        """Detect and apply any runtime tuning changes to the PI controller."""

        # Proportional band or integral time changed
        if resolved.proportional_band != self._last_prop_band or resolved.integral_time != self._last_int_time:
            self._pi.update_tunings(resolved.proportional_band, resolved.integral_time)
            self._last_prop_band = resolved.proportional_band
            self._last_int_time = resolved.integral_time
            self._logger.info(
                "Tunings updated: prop_band=%s K, int_time=%s min",
                resolved.proportional_band,
                resolved.integral_time,
            )

        # Output limits changed
        if resolved.output_min != self._last_output_min or resolved.output_max != self._last_output_max:
            self._pi.update_output_limits(resolved.output_min, resolved.output_max)
            self._last_output_min = resolved.output_min
            self._last_output_max = resolved.output_max
            self._logger.info(
                "Output limits updated: min=%s, max=%s",
                resolved.output_min,
                resolved.output_max,
            )

        # Update interval changed
        if resolved.update_interval != self._last_update_interval:
            new_interval = resolved.update_interval
            self._pi.update_sample_time(float(new_interval))
            self.update_interval = timedelta(seconds=new_interval)
            self._last_update_interval = new_interval
            self._logger.info("Update interval changed to %s s", new_interval)

    #
    # _determine_cooling
    #
    def _determine_cooling(self, resolved: ResolvedConfig) -> bool:
        """Determine whether the controller should operate in cooling mode.

        Returns:
            ``True`` if cooling, ``False`` if heating.
        """

        mode = resolved.operating_mode

        if mode == OperatingMode.COOL:
            return True
        if mode == OperatingMode.HEAT:
            return False

        # heat_cool → read from climate entity
        if resolved.climate_entity:
            action = self._ha.get_climate_hvac_action(resolved.climate_entity)
            if action == HVACAction.COOLING:
                return True
            if action == HVACAction.HEATING:
                return False

            hvac_mode = self._ha.get_climate_hvac_mode(resolved.climate_entity)
            if hvac_mode == HVACMode.COOL:
                return True
            if hvac_mode == HVACMode.HEAT:
                return False

        # Fall back to heating when neither action nor mode resolves direction.
        return False

    # ------------------------------------------------------------------
    # Core update loop
    # ------------------------------------------------------------------

    #
    # _async_update_data
    #
    async def _async_update_data(self) -> CoordinatorData:
        """Run one PI control cycle.

        This is called by HA's DataUpdateCoordinator on every update interval,
        on first refresh, and on manual refresh requests.

        Returns:
            ``CoordinatorData`` consumed by all entities.

        Raises:
            UpdateFailed: On critical configuration errors.
        """

        # ── Step 1: Resolve config ──────────────────────────────────────
        try:
            resolved = self._resolve()
        except Exception as err:
            self._logger.error("Configuration error: %s", err)
            raise UpdateFailed(f"Configuration error: {err}") from err

        # ── Step 1b: Check enabled flag ────────────────────────────────
        if not resolved.enabled:
            if resolved.control_mode == ControlMode.CCA:
                self._logger.debug("CCA controller disabled via enabled flag; forcing output to 0")
                data = await self._async_update_disabled_cca_data(resolved)
                self._last_data = data
                return data

            self._logger.debug("PI controller paused via enabled flag")
            return self._paused_result()

        if resolved.control_mode == ControlMode.CCA:
            data = await self._async_update_cca_data(resolved)
            self._last_data = data
            return data

        data = await self._async_update_pi_data(resolved)
        self._last_data = data
        return data

    #
    # _async_update_pi_data
    #
    async def _async_update_pi_data(self, resolved: ResolvedConfig) -> CoordinatorData:
        """Run one PI control cycle."""

        # ── Step 2: Auto-disable on HVAC off ────────────────────────────
        if resolved.climate_entity and resolved.auto_disable_on_hvac_off:
            hvac_mode = self._ha.get_climate_hvac_mode(resolved.climate_entity)
            if hvac_mode == HVACMode.OFF:
                self._logger.debug("Auto-disabled: climate entity hvac_mode is off")
                return self._shutdown_result()

        # ── Step 3: Determine heating / cooling direction ───────────────
        is_cooling = self._determine_cooling(resolved)
        self._pi.set_cooling(is_cooling)
        current_mode = self._current_mode(is_cooling)

        # ── Step 4: Read current temperature ────────────────────────────
        current_temp = self._read_current_temp(resolved)

        # ── Step 5: Determine target temperature ────────────────────────
        target_temp = self._read_target_temp(resolved)

        # ── Step 6: Handle sensor faults ────────────────────────────────
        if current_temp is None:
            return self._handle_fault_mode(
                resolved,
                current_temp=None,
                target_temp=target_temp,
                sensor_available=False,
                reason="Sensor",
            )

        if target_temp is None and resolved.target_temp_mode != TargetTempMode.INTERNAL:
            return self._handle_fault_mode(
                resolved,
                current_temp=current_temp,
                target_temp=None,
                sensor_available=True,
                reason="Target temperature",
            )

        if target_temp is not None:
            self._pi.set_target(target_temp)

        # Sensor is OK — reset fault counter
        self._fault_cycles = 0

        # ── Step 7: Apply tuning changes ────────────────────────────────
        self._apply_tuning_changes(resolved)

        # ── Step 8: Run PI controller ───────────────────────────────────
        result = self._pi.update(current_temp)

        # Track last good output for HOLD fault mode
        self._last_good_output = result.output

        # ── Step 9: Return CoordinatorData ──────────────────────────────
        data = CoordinatorData(
            output=result.output,
            deviation=result.deviation,
            current_mode=current_mode,
            p_term=result.p_term,
            i_term=result.i_term,
            current_temp=current_temp,
            target_temp=target_temp,
            sensor_available=True,
        )
        return data

    #
    # _async_update_cca_data
    #
    async def _async_update_cca_data(self, resolved: ResolvedConfig) -> CoordinatorData:
        """Run one CCA control cycle."""

        cooling_enabled = False
        if resolved.cca_cooling_enable_entity:
            cooling_signal_on = self._ha.get_entity_on_state(resolved.cca_cooling_enable_entity)
            if cooling_signal_on is not None:
                self._cca_restore_gate_grace = False
                cooling_enabled = cooling_signal_on if resolved.cca_cooling_enable_on else not cooling_signal_on
            elif self._consume_cca_restore_gate_grace():
                await self._async_apply_cca_refresh_state(resolved)
                return self._build_cca_refresh_result(resolved)

        if (
            cooling_enabled
            and not self._cca_runtime_forecast_refresh_pending
            and not self._should_recompute_cca_on_gate_enable()
            and not self._is_cca_update_due(resolved)
        ):
            await self._async_apply_cca_refresh_state(resolved)
            return self._build_cca_refresh_result(resolved)

        forecasts: list[dict[str, Any]] | None = None
        if cooling_enabled and resolved.cca_weather_entity:
            try:
                forecasts = await self._ha.async_get_daily_forecasts(resolved.cca_weather_entity)
            except Exception as err:
                self._logger.warning("CCA forecast retrieval failed: %s", err)

        previous_state = self._cca.get_state()
        should_recompute_now = (
            cooling_enabled
            and self._cca_runtime_forecast_refresh_pending
            and not self._should_recompute_cca_on_gate_enable()
            and not self._is_cca_update_due(resolved)
        )

        if should_recompute_now:
            result = self._cca.recompute_now(
                resolved,
                cooling_enabled=cooling_enabled,
                forecasts=forecasts,
            )
        else:
            result = self._cca.compute_step(
                resolved,
                cooling_enabled=cooling_enabled,
                forecasts=forecasts,
            )

        self._cca_runtime_forecast_refresh_pending = False

        if result.state != previous_state:
            await self._async_save_cca_state(result.state)

        return CoordinatorData(
            output=result.output,
            current_mode=result.current_mode,
            sensor_available=True,
            cca_heat_score=result.heat_score,
            cca_charge_estimate=result.charge_estimate,
            cca_charge_target=result.charge_target,
            cca_override_active=result.override_active,
            cca_status=result.status,
            cca_next_update_in=self._cca_next_update_in_minutes(resolved) if cooling_enabled else None,
        )

    #
    # _async_update_disabled_cca_data
    #
    async def _async_update_disabled_cca_data(self, resolved: ResolvedConfig) -> CoordinatorData:
        """Return the disabled-state CCA payload and persist the inactive state."""

        previous_state = self._cca.get_state()
        result = self._cca.recompute_now(
            resolved,
            cooling_enabled=False,
            forecasts=None,
        )

        if result.state != previous_state:
            await self._async_save_cca_state(result.state)

        return CoordinatorData(
            output=result.output,
            current_mode=result.current_mode,
            sensor_available=True,
            cca_heat_score=result.heat_score,
            cca_charge_estimate=result.charge_estimate,
            cca_charge_target=result.charge_target,
            cca_override_active=result.override_active,
            cca_status=result.status,
            cca_next_update_in=None,
        )

    #
    # _handle_fault_mode
    #
    def _handle_fault_mode(
        self,
        resolved: ResolvedConfig,
        *,
        current_temp: float | None,
        target_temp: float | None,
        sensor_available: bool,
        reason: str,
    ) -> CoordinatorData:
        """Apply the configured fault policy for a missing controller input.

        Args:
            resolved: Current resolved configuration.
            current_temp: Current measured temperature, if available.
            target_temp: Current target temperature (may be ``None``).
            sensor_available: Whether the temperature sensor is available.
            reason: Short label used in log messages.

        Returns:
            ``CoordinatorData`` with either held output or shutdown (output=0).
        """

        fault_mode = resolved.sensor_fault_mode

        if fault_mode == SensorFaultMode.HOLD and self._last_good_output is not None:
            grace_cycles = max(
                1,
                SENSOR_FAULT_GRACE_PERIOD_SECONDS // max(resolved.update_interval, 1),
            )

            self._fault_cycles += 1

            if self._fault_cycles <= grace_cycles:
                self._logger.warning(
                    "%s unavailable (cycle %s/%s) — holding last output %s",
                    reason,
                    self._fault_cycles,
                    grace_cycles,
                    self._last_good_output,
                )
                return CoordinatorData(
                    output=self._last_good_output,
                    deviation=None,
                    current_mode=self._last_data.current_mode if self._last_data is not None else None,
                    p_term=None,
                    i_term=None,
                    current_temp=current_temp,
                    target_temp=target_temp,
                    sensor_available=sensor_available,
                )

            # Grace period exceeded — fall through to shutdown
            self._logger.warning("%s unavailable — grace period exceeded, shutting down output", reason)

        elif fault_mode == SensorFaultMode.HOLD:
            # HOLD mode but no prior good output (e.g. first cycle after restart).
            # Return unknown result so entity states are not changed from their
            # restored values — avoids sending a spurious 0 % on restart.
            self._logger.info("%s unavailable — no prior output available, waiting", reason)
            return self._unknown_result(
                current_temp=current_temp,
                target_temp=target_temp,
                sensor_available=sensor_available,
            )

        else:
            self._logger.warning("%s unavailable — shutting down output (shutdown mode)", reason)

        return self._shutdown_result(
            current_temp=current_temp,
            target_temp=target_temp,
            sensor_available=sensor_available,
        )
