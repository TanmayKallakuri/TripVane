"""Event models: the contract between sensors and everything downstream.

Every event is immutable and rejects unknown fields, so a sensor cannot store more
than the schema allows.
"""

from datetime import UTC
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    field_validator,
    model_validator,
)

from tripvane_core.hashing import payload_hash as compute_payload_hash

# The only request headers a source block may carry, in canonical spelling.
ALLOWED_HEADERS = ("Accept-Language", "Content-Type", "Referer", "X-Forwarded-For")
_CANONICAL_HEADERS = {name.lower(): name for name in ALLOWED_HEADERS}

UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Channel = Literal["http", "mcp", "github", "email"]
EndReason = Literal["completed", "gate_rejected", "budget_exhausted", "error"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Frozen):
    ip: IPvAnyAddress
    asn: int | None = Field(default=None, ge=0, le=2**32 - 1)
    user_agent: str | None = None
    headers_subset: dict[str, str] = Field(default_factory=dict)
    account_handle: str | None = None

    @field_validator("headers_subset")
    @classmethod
    def _only_allowed_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        canonical: dict[str, str] = {}
        for name, value in headers.items():
            key = _CANONICAL_HEADERS.get(name.lower())
            if key is None:
                raise ValueError(f"header {name!r} is not in {ALLOWED_HEADERS}")
            canonical[key] = value
        return canonical


class _EventBase(_Frozen):
    sensor_id: Identifier
    session_id: Identifier
    event_seq: int = Field(ge=0)
    ts: UtcDatetime
    source: Source


class SessionStarted(_EventBase):
    type: Literal["session_started"] = "session_started"


class InputReceived(_EventBase):
    type: Literal["input_received"] = "input_received"
    raw_text: str
    channel: Channel
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _hash_matches_text(self) -> Self:
        if self.payload_hash != compute_payload_hash(self.raw_text):
            raise ValueError("payload_hash does not match payload_hash(raw_text)")
        return self


class ModelTurn(_EventBase):
    type: Literal["model_turn"] = "model_turn"
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    stop_reason: str
    assistant_text: str


class ToolCallAttempted(_EventBase):
    type: Literal["tool_call_attempted"] = "tool_call_attempted"
    tool_name: str
    arguments: dict[str, Any]
    fake_result: str


class SessionEnded(_EventBase):
    type: Literal["session_ended"] = "session_ended"
    reason: EndReason


Event = Annotated[
    SessionStarted | InputReceived | ModelTurn | ToolCallAttempted | SessionEnded,
    Field(discriminator="type"),
]
