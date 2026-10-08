from datetime import date

import pytest

from tripvane_core.config import Settings
from tripvane_sensors.runtime.budget import Budget


def test_refuses_once_the_daily_limit_is_reached() -> None:
    budget = Budget(100)
    assert budget.allows_call()
    budget.record(60)
    assert budget.allows_call()
    budget.record(40)
    assert budget.spent == 100
    assert not budget.allows_call()


def test_zero_budget_refuses_every_call() -> None:
    assert not Budget(0).allows_call()


def test_spend_resets_on_a_new_utc_day() -> None:
    day = [date(2026, 10, 8)]
    budget = Budget(100, today=lambda: day[0])
    budget.record(150)
    assert not budget.allows_call()
    day[0] = date(2026, 10, 9)
    assert budget.spent == 0
    assert budget.allows_call()


def test_from_settings_reads_daily_token_budget() -> None:
    budget = Budget.from_settings(Settings.from_env({"DAILY_TOKEN_BUDGET": "5000"}))
    assert budget.daily_limit == 5000


def test_from_settings_requires_daily_token_budget() -> None:
    with pytest.raises(RuntimeError, match="DAILY_TOKEN_BUDGET"):
        Budget.from_settings(Settings.from_env({}))


def test_rejects_negative_values() -> None:
    with pytest.raises(ValueError):
        Budget(-1)
    with pytest.raises(ValueError):
        Budget(10).record(-5)
