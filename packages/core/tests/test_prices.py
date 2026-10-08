from decimal import Decimal

import pytest

from tripvane_core.prices import CHECKED_ON, PRICES, UnknownModel, cost, price


def test_the_plan_models_have_prices() -> None:
    assert set(PRICES) == {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
    assert CHECKED_ON.isoformat() == "2026-10-08"


def test_input_and_output_per_million() -> None:
    assert cost("claude-haiku-5-5", input_tokens=1_000_000, output_tokens=1_000_000) == Decimal(
        "0.60"
    )
    assert cost("claude-opus-5-5", input_tokens=10_000, output_tokens=2_000) == Decimal("0.08")
    assert cost("claude-sonnet-5-5", input_tokens=500_000, output_tokens=0) == Decimal("1")


def test_cache_writes_and_reads_have_their_own_prices() -> None:
    assert cost(
        "claude-haiku-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=200_000,
        cache_read_tokens=500_000,
    ) == Decimal("0.03")
    assert cost(
        "claude-sonnet-5-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    ) == Decimal("0.10")


def test_long_prompt_price_applies_to_haiku_only() -> None:
    assert cost(
        "claude-haiku-5-5", input_tokens=150_000, output_tokens=1_000, long_prompt=True
    ) == Decimal("0.0775")
    assert price("claude-sonnet-5-5", long_prompt=True) == price("claude-sonnet-5-5")


def test_unknown_model_fails_loudly() -> None:
    with pytest.raises(UnknownModel, match="claude-unknown"):
        cost("claude-unknown", input_tokens=1, output_tokens=1)
