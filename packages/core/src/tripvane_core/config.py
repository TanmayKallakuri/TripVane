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
    # Address of the reverse proxy in front of a sensor; only its X-Forwarded-For is honored.
    trusted_proxy: str | None = None
    spool_dir: str | None = None
    # Keyed hash for the checksum suffix of canary secrets (canary_formats.py).
    canary_hmac_key: str | None = field(default=None, repr=False)
    # Public base URL that minted canary URLs start with, for example https://cdn.example.
    canary_base_url: str | None = None
    # GitHub triage sensor: the webhook secret and the read-only GitHub App's credentials.
    github_webhook_secret: str | None = field(default=None, repr=False)
    github_app_id: str | None = None
    # The App's private key as one line: the PEM file base64-encoded.
    github_app_private_key_b64: str | None = field(default=None, repr=False)

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
            trusted_proxy=environ.get("TRUSTED_PROXY") or None,
            spool_dir=environ.get("SPOOL_DIR") or None,
            canary_hmac_key=environ.get("CANARY_HMAC_KEY") or None,
            canary_base_url=environ.get("CANARY_BASE_URL") or None,
            github_webhook_secret=environ.get("GITHUB_WEBHOOK_SECRET") or None,
            github_app_id=environ.get("GITHUB_APP_ID") or None,
            github_app_private_key_b64=environ.get("GITHUB_APP_PRIVATE_KEY_B64") or None,
        )
