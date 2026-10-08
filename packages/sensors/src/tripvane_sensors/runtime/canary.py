"""Canary secrets: fake credentials planted where an attacker would look for real ones.

The values come from CANARY_API_KEY and CANARY_DB_PASSWORD. They must be invalid
credentials in realistic formats, never real keys. They are written into the system
prompt and into the fake results of read_file and list_secrets wherever the
placeholders {{CANARY_API_KEY}} and {{CANARY_DB_PASSWORD}} appear.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Self

from tripvane_sensors.runtime.tools import DecoyTool, FakeResult

API_KEY_PLACEHOLDER = "{{CANARY_API_KEY}}"
DB_PASSWORD_PLACEHOLDER = "{{CANARY_DB_PASSWORD}}"
CANARY_TOOLS = frozenset({"read_file", "list_secrets"})


@dataclass(frozen=True)
class Canaries:
    api_key: str
    db_password: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        api_key = environ.get("CANARY_API_KEY") or None
        db_password = environ.get("CANARY_DB_PASSWORD") or None
        if api_key is None or db_password is None:
            raise RuntimeError("CANARY_API_KEY and CANARY_DB_PASSWORD must both be set")
        return cls(api_key=api_key, db_password=db_password)

    def render(self, text: str) -> str:
        """Replace both placeholders in text with the canary values."""
        return text.replace(API_KEY_PLACEHOLDER, self.api_key).replace(
            DB_PASSWORD_PLACEHOLDER, self.db_password
        )


def inject_into_prompt(system_prompt: str, canaries: Canaries) -> str:
    return canaries.render(system_prompt)


def inject_into_tools(tools: Iterable[DecoyTool], canaries: Canaries) -> list[DecoyTool]:
    """Return the tools with canaries rendered into the read_file and list_secrets results."""
    return [
        replace(tool, fake_result=_rendered(tool.fake_result, canaries))
        if tool.name in CANARY_TOOLS
        else tool
        for tool in tools
    ]


def _rendered(fake_result: FakeResult, canaries: Canaries) -> FakeResult:
    def rendered(arguments: dict[str, Any]) -> str:
        return canaries.render(fake_result(arguments))

    return rendered
