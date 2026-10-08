"""Settings read from environment variables only. There are no config files.

Every value is optional here because each process needs a different subset (the
collector needs DATABASE_URL, a sensor needs the rest). The code that uses a value
fails loudly when it is missing.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self


@dataclass(frozen=True)
class Settings:
    database_url: str | None = field(default=None, repr=False)
    collector_url: str | None = None
    collector_token: str | None = field(default=None, repr=False)
    anthropic_api_key: str | None = field(default=None, repr=False)
    sensor_id: str | None = None
    daily_token_budget: int | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        raw_budget = environ.get("DAILY_TOKEN_BUDGET") or None
        if raw_budget is not None and not (raw_budget.isascii() and raw_budget.isdigit()):
            raise ValueError("DAILY_TOKEN_BUDGET must be a non-negative integer")
        budget = int(raw_budget) if raw_budget is not None else None
        return cls(
            database_url=environ.get("DATABASE_URL") or None,
            collector_url=environ.get("COLLECTOR_URL") or None,
            collector_token=environ.get("COLLECTOR_TOKEN") or None,
            anthropic_api_key=environ.get("ANTHROPIC_API_KEY") or None,
            sensor_id=environ.get("SENSOR_ID") or None,
            daily_token_budget=budget,
        )
