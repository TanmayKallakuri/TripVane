import pytest

from tripvane_core.config import Settings


def test_reads_every_variable_from_the_environment() -> None:
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://user:pw@db/tripvane",
            "COLLECTOR_URL": "https://collector.example.test",
            "COLLECTOR_TOKEN": "collector-token",
            "ANTHROPIC_API_KEY": "not-a-real-key",
            "SENSOR_ID": "support-1",
            "DAILY_TOKEN_BUDGET": "200000",
        }
    )
    assert settings == Settings(
        database_url="postgresql://user:pw@db/tripvane",
        collector_url="https://collector.example.test",
        collector_token="collector-token",
        anthropic_api_key="not-a-real-key",
        sensor_id="support-1",
        daily_token_budget=200_000,
    )


def test_unset_and_empty_variables_are_none() -> None:
    assert Settings.from_env({"SENSOR_ID": ""}) == Settings()


@pytest.mark.parametrize("value", ["-5", "1e6", "ten", "²"])
def test_daily_token_budget_must_be_a_non_negative_integer(value: str) -> None:
    with pytest.raises(ValueError, match="DAILY_TOKEN_BUDGET"):
        Settings.from_env({"DAILY_TOKEN_BUDGET": value})


def test_repr_does_not_leak_secrets() -> None:
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://user:db-password@db/tripvane",
            "COLLECTOR_TOKEN": "collector-token",
            "ANTHROPIC_API_KEY": "not-a-real-key",
        }
    )
    text = repr(settings)
    assert "db-password" not in text
    assert "collector-token" not in text
    assert "not-a-real-key" not in text
