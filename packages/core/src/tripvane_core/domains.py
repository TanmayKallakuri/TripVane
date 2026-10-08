"""Domain extraction and normalization, shared by the collector and the lookup API.

Only hosts that appear in a URL (scheme://host) or an email address (user@host) are
extracted. Bare words with dots ("config.yaml") are ignored to avoid false positives.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

_URL_HOST = re.compile(r"[a-z][a-z0-9+.\-]*://(?:[^\s/?#@]*@)?([^\s/?#:\[\]@\"'<>()]+)", re.I)
_EMAIL_HOST = re.compile(r"[a-z0-9._%+\-]+@([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)", re.I)
_LABEL = re.compile(r"^(?!-)[a-z0-9\-]{1,63}(?<!-)$")


def normalize_domain(host: str) -> str | None:
    """Return the lowercase ASCII form of a hostname, or None if it is not a domain.

    IP addresses, single-label names and syntactically invalid hosts return None.
    """
    host = host.strip().rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2:
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None
    if not labels[-1].replace("xn--", "").isalpha():
        return None
    return host


def extract_domains(text: str) -> set[str]:
    """Domains of every URL and email address in the text."""
    hosts = [m.group(1) for m in _URL_HOST.finditer(text)]
    hosts += [m.group(1) for m in _EMAIL_HOST.finditer(text)]
    return {domain for host in hosts if (domain := normalize_domain(host)) is not None}


def extract_domains_from_value(value: Any) -> set[str]:  # noqa: ANN401
    """Domains in every string inside a JSON-like value (tool call arguments)."""
    return {domain for text in _strings(value) for domain in extract_domains(text)}


def _strings(value: Any) -> Iterator[str]:  # noqa: ANN401
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, Iterable):
        for item in value:
            yield from _strings(item)
