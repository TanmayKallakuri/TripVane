"""Daily token budget for one sensor, kept in memory and reset at midnight UTC."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Self

from tripvane_core.config import Settings


def _today() -> date:
    return datetime.now(UTC).date()


class Budget:
    """Counts tokens spent today and refuses model calls once the daily limit is reached.

    Spend is every input token (uncached, cache write and cache read) plus every output
    token. The check happens before a call, so the last call of the day can overshoot the
    limit by at most one call's usage.
    """

    def __init__(self, daily_limit: int, today: Callable[[], date] = _today) -> None:
        if daily_limit < 0:
            raise ValueError("daily_limit must be non-negative")
        self.daily_limit = daily_limit
        self._today = today
        self._day = today()
        self._spent = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.daily_token_budget is None:
            raise RuntimeError("DAILY_TOKEN_BUDGET is not set")
        return cls(settings.daily_token_budget)

    @property
    def spent(self) -> int:
        self._roll_over()
        return self._spent

    def allows_call(self) -> bool:
        """True while today's spend is below the daily limit."""
        return self.spent < self.daily_limit

    def record(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        self._roll_over()
        self._spent += tokens

    def _roll_over(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._spent = 0
