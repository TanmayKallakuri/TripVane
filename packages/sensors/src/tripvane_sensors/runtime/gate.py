"""The cheap gate: decides, without a model call, whether input is worth a model call."""

from collections import deque

from tripvane_core.hashing import payload_hash

MIN_LENGTH = 8
RECENT_HASHES_SIZE = 50


def new_recent_hashes() -> deque[str]:
    """The bounded record of recently accepted payload hashes for one process."""
    return deque(maxlen=RECENT_HASHES_SIZE)


def cheap_gate(text: str, recent_hashes: deque[str]) -> bool:
    """Return True when the model should be called for this text.

    Rejects empty input, input under MIN_LENGTH characters once surrounding whitespace is
    stripped, and any payload whose hash is already in recent_hashes. An accepted hash is
    appended, so a repeat of the same payload is rejected until it ages out.
    """
    if len(text.strip()) < MIN_LENGTH:
        return False
    digest = payload_hash(text)
    if digest in recent_hashes:
        return False
    recent_hashes.append(digest)
    return True
