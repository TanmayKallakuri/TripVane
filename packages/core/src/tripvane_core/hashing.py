import hashlib


def normalize(text: str) -> str:
    """Lowercase, collapse runs of whitespace to one space, strip both ends."""
    return " ".join(text.lower().split())


def payload_hash(text: str) -> str:
    """Hex sha256 of the normalized text."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
