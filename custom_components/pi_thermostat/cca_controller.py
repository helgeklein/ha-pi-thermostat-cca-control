"""CCA controller strategy for forecast-driven cooling control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .config import ResolvedConfig


@dataclass(frozen=True, slots=True)
class CCAState:
    """Persisted runtime state for the CCA controller."""

    charge_estimate: float = 0.0
    last_auto_output: float = 0.0
    last_heat_score: float = 0.0
    last_step_timestamp_iso: str | None = None
    status: str = "idle"


@dataclass(frozen=True, slots=True)
class CCAControllerResult:
    """Computed result of one CCA control cycle."""

    output: float
    current_mode: str
    heat_score: float | None
    charge_target: float | None
    charge_estimate: float
    override_active: str
    status: str
    state: CCAState


class CCAControllerStrategy:
    """Forecast-driven controller for concrete core activation cooling."""

    def __init__(self) -> None:
        """Initialize the controller with empty persisted state."""

        self._state = CCAState()

    @staticmethod
    def _clip(value: float, lower: float, upper: float) -> float:
        """Clamp a value to the provided range."""

        return max(lower, min(upper, value))

    @classmethod
    def compute_charge_target(cls, heat_score: float, resolved: ResolvedConfig) -> float:
        """Map a normalized heat score to a normalized charge target."""

        return cls._clip(
            heat_score * (resolved.cca_charge_target_scale / 100.0),
            0.0,
            100.0,
        )

    @staticmethod
    def _parse_forecast_date(value: Any) -> date | None:
        """Parse a forecast date or datetime value."""

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        if not isinstance(value, str):
            return None

        normalized = value.replace("Z", "+00:00")

        try:
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None

    @staticmethod
    def _extract_forecast_high(forecast: dict[str, Any]) -> float | None:
        """Extract the daily high temperature from a forecast payload."""

        value = forecast.get("temperature")
        try:
            return float(value) if value is not None else None
        except TypeError, ValueError:
            return None

    @staticmethod
    def _extract_forecast_low(forecast: dict[str, Any]) -> float | None:
        """Extract the daily low temperature from a forecast payload."""

        value = forecast.get("templow")
        try:
            return float(value) if value is not None else None
        except TypeError, ValueError:
            return None

    def restore_state(self, state: CCAState) -> None:
        """Restore persisted state after restart."""

        self._state = state

    def get_state(self) -> CCAState:
        """Return the current internal controller state."""

        return self._state

    def _valid_forecasts(
        self,
        forecasts: list[dict[str, Any]],
        horizon_days: int,
    ) -> list[tuple[date, float, float]]:
        """Return valid daily forecast tuples limited to the requested horizon."""

        valid: list[tuple[date, float, float]] = []

        for forecast in forecasts:
            forecast_date = self._parse_forecast_date(forecast.get("datetime") or forecast.get("date"))
            high = self._extract_forecast_high(forecast)
            low = self._extract_forecast_low(forecast)

            if forecast_date is None or high is None or low is None:
                continue

            valid.append((forecast_date, high, low))

        valid.sort(key=lambda item: item[0])
        return valid[: max(horizon_days, 1)]

    def _compute_heat_score(
        self,
        valid_forecasts: list[tuple[date, float, float]],
        resolved: ResolvedConfig,
    ) -> float:
        """Compute a normalized 0-100 heat score from daily highs and lows."""

        if not valid_forecasts:
            return 0.0

        scores: list[float] = []

        for _, high, low in valid_forecasts:
            hot_day_score = self._clip(
                (high - resolved.cca_hot_day_threshold) * 12.5,
                0.0,
                100.0,
            )
            warm_night_score = self._clip(
                (low - resolved.cca_warm_night_threshold) * 20.0,
                0.0,
                100.0,
            )
            scores.append(self._clip(hot_day_score * 0.7 + warm_night_score * 0.3, 0.0, 100.0))

        return sum(scores) / len(scores)

    def _compute_with_forecasts(
        self,
        resolved: ResolvedConfig,
        *,
        cooling_enabled: bool,
        forecasts: list[dict[str, Any]] | None,
        advance_step: bool,
    ) -> CCAControllerResult:
        """Compute one CCA control cycle from forecasts and current settings."""

        now_iso = datetime.now(UTC).isoformat()

        if not cooling_enabled:
            charge_target = self.compute_charge_target(self._state.last_heat_score, resolved)
            state = CCAState(
                charge_estimate=self._state.charge_estimate,
                last_auto_output=0.0,
                last_heat_score=self._state.last_heat_score,
                last_step_timestamp_iso=self._state.last_step_timestamp_iso,
                status="inactive",
            )
            self._state = state
            return CCAControllerResult(
                output=0.0,
                current_mode="off",
                heat_score=self._state.last_heat_score,
                charge_target=charge_target,
                charge_estimate=state.charge_estimate,
                override_active="off",
                status=state.status,
                state=state,
            )

        if forecasts is None:
            charge_target = self.compute_charge_target(self._state.last_heat_score, resolved)
            if resolved.cca_forecast_unavailable_mode == "hold":
                output = self._state.last_auto_output
                status = "forecast_hold"
            else:
                output = 0.0
                status = "forecast_unavailable"

            state = CCAState(
                charge_estimate=self._state.charge_estimate,
                last_auto_output=output,
                last_heat_score=self._state.last_heat_score,
                last_step_timestamp_iso=(now_iso if advance_step else self._state.last_step_timestamp_iso),
                status=status,
            )
            self._state = state
            return CCAControllerResult(
                output=output,
                current_mode="cooling" if output > 0 else "off",
                heat_score=self._state.last_heat_score,
                charge_target=charge_target,
                charge_estimate=state.charge_estimate,
                override_active="off",
                status=status,
                state=state,
            )

        valid_forecasts = self._valid_forecasts(
            forecasts,
            resolved.cca_forecast_horizon_days,
        )

        if not valid_forecasts:
            return self._compute_with_forecasts(
                resolved,
                cooling_enabled=cooling_enabled,
                forecasts=None,
                advance_step=advance_step,
            )

        heat_score = self._compute_heat_score(valid_forecasts, resolved)
        charge_target = self.compute_charge_target(heat_score, resolved)
        if advance_step:
            charge_estimate = self._clip(
                self._state.charge_estimate
                + resolved.cca_charge_gain * (self._state.last_auto_output / 100.0)
                - resolved.cca_discharge_gain * (charge_target / 100.0),
                0.0,
                100.0,
            )
        else:
            charge_estimate = self._state.charge_estimate

        requested_output = self._clip(charge_target - charge_estimate, 0.0, 100.0)

        delta = requested_output - self._state.last_auto_output
        if delta > resolved.cca_output_step_limit:
            auto_output = self._state.last_auto_output + resolved.cca_output_step_limit
        elif delta < -resolved.cca_output_step_limit:
            auto_output = self._state.last_auto_output - resolved.cca_output_step_limit
        else:
            auto_output = requested_output

        auto_output = self._clip(auto_output, 0.0, 100.0)
        if auto_output > 0.0:
            auto_output = self._clip(
                auto_output,
                resolved.cca_output_min,
                resolved.cca_output_max,
            )

        final_output = auto_output
        override_active = "off"
        status = "active"

        if resolved.cca_manual_override_enabled:
            final_output = self._clip(resolved.cca_manual_output, 0.0, 100.0)
            override_active = "on"
            status = "manual_override"

        state = CCAState(
            charge_estimate=charge_estimate,
            last_auto_output=auto_output,
            last_heat_score=heat_score,
            last_step_timestamp_iso=now_iso if advance_step else self._state.last_step_timestamp_iso,
            status=status,
        )
        self._state = state

        return CCAControllerResult(
            output=final_output,
            current_mode="cooling" if final_output > 0 else "off",
            heat_score=heat_score,
            charge_target=charge_target,
            charge_estimate=charge_estimate,
            override_active=override_active,
            status=status,
            state=state,
        )

    def compute_step(
        self,
        resolved: ResolvedConfig,
        *,
        cooling_enabled: bool,
        forecasts: list[dict[str, Any]] | None,
    ) -> CCAControllerResult:
        """Compute a scheduled CCA control step and advance persisted step timing."""

        return self._compute_with_forecasts(
            resolved,
            cooling_enabled=cooling_enabled,
            forecasts=forecasts,
            advance_step=True,
        )

    def recompute_now(
        self,
        resolved: ResolvedConfig,
        *,
        cooling_enabled: bool,
        forecasts: list[dict[str, Any]] | None,
    ) -> CCAControllerResult:
        """Recompute the current automatic output without consuming the scheduled step."""

        return self._compute_with_forecasts(
            resolved,
            cooling_enabled=cooling_enabled,
            forecasts=forecasts,
            advance_step=False,
        )
