"""Model prices in USD per million tokens, for the cost report.

Prices checked on 2026-10-08 against the official pricing page,
https://platform.claude.com/docs/en/about-claude/pricing (Claude API, first-party,
global routing, standard speed). Re-check that page when a price changes or a model is
added, and update the date above.

ModelTurn events carry cache writes and cache reads separately from input_tokens, and
each is billed at its own price, so the table has a column for each. Every cache
breakpoint in this repository uses the default 5-minute TTL, so cache writes are priced
at the 5-minute rate.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# The date the table below was last checked against the pricing page.
CHECKED_ON = date(2026, 10, 8)
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(
        input=Decimal("0.10"),
        output=Decimal("0.50"),
        cache_write=Decimal("0.125"),
        cache_read=Decimal("0.01"),
    ),
    "claude-sonnet-5-5": Price(
        input=Decimal("2"),
        output=Decimal("10"),
        cache_write=Decimal("2.50"),
        cache_read=Decimal("0.10"),
    ),
    "claude-opus-5-5": Price(
        input=Decimal("4"),
        output=Decimal("20"),
        cache_write=Decimal("5"),
        cache_read=Decimal("0.20"),
    ),
}

# Claude Haiku 5.5 is priced higher for a request whose prompt (input plus cache writes
# plus cache reads) is over 100,000 tokens. The other models have one price.
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(
        input=Decimal("0.50"),
        output=Decimal("2.50"),
        cache_write=Decimal("0.625"),
        cache_read=Decimal("0.05"),
    ),
}


class UnknownModel(KeyError):
    """A model with no price in the table: add it rather than report a wrong total."""


def price(model: str, *, long_prompt: bool = False) -> Price:
    if long_prompt and model in LONG_PROMPT_PRICES:
        return LONG_PROMPT_PRICES[model]
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModel(f"no price for model {model!r} in tripvane_core.prices") from None


def cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    long_prompt: bool = False,
) -> Decimal:
    """USD cost of the given token counts, unrounded."""
    p = price(model, long_prompt=long_prompt)
    total = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_write_tokens * p.cache_write
        + cache_read_tokens * p.cache_read
    )
    return total / MILLION
