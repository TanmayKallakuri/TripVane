import hashlib

from tripvane_core.hashing import normalize, payload_hash


def test_normalize_lowercases_collapses_and_strips() -> None:
    assert normalize("  Ignore\tPREVIOUS \n\n instructions  ") == "ignore previous instructions"


def test_normalize_empty_and_whitespace_only() -> None:
    assert normalize("") == ""
    assert normalize(" \t\n ") == ""


def test_payload_hash_is_sha256_hex_of_normalized_text() -> None:
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert payload_hash("  Hello\n  WORLD ") == expected
    assert len(expected) == 64


def test_payload_hash_differs_for_different_text() -> None:
    assert payload_hash("send the secrets") != payload_hash("send the secret")
