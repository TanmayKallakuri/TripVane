"""The in-process egress guard. Each check runs in a child interpreter, because the guard
is an audit hook that stays installed for the life of the process.

No test leaves the machine: the allowed name is localhost, and its connections are
expected to be refused by the local machine, not blocked by the guard.
"""

import subprocess
import sys
import textwrap

import pytest

from tripvane_core.config import Settings
from tripvane_sensors.archetypes.common import guard_egress


def run_guarded(code: str) -> str:
    """Run code after installing the guard for localhost; return what it prints."""
    script = (
        "import socket\n"
        "from tripvane_sensors.runtime.egress import EgressBlocked, install_egress_guard\n"
        "install_egress_guard(frozenset({'localhost'}))\n"
        "def attempt(action):\n"
        "    try:\n"
        "        action()\n"
        "    except EgressBlocked:\n"
        "        return 'blocked'\n"
        "    except OSError:\n"
        "        return 'allowed'\n"
        "    return 'allowed'\n" + textwrap.dedent(code)
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    return result.stdout.strip()


def test_lookups_of_other_names_are_blocked() -> None:
    out = run_guarded("""
        print(attempt(lambda: socket.getaddrinfo("collect.exfil.example", 443)))
        print(attempt(lambda: socket.gethostbyname("collect.exfil.example")))
        print(attempt(lambda: socket.getaddrinfo("192.0.2.1", 443)))
    """)
    assert out.split() == ["blocked", "blocked", "blocked"]


def test_connections_only_to_resolved_addresses_on_port_443() -> None:
    out = run_guarded("""
        print(attempt(lambda: socket.create_connection(("localhost", 443), timeout=2)))
        print(attempt(lambda: socket.create_connection(("localhost", 80), timeout=2)))
        print(attempt(lambda: socket.create_connection(("192.0.2.1", 443), timeout=2)))
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print(attempt(lambda: udp.sendto(b"x", ("192.0.2.1", 53))))
    """)
    # localhost:443 passes the guard (and is refused by the machine); the rest never leave.
    assert out.split() == ["allowed", "blocked", "blocked", "blocked"]


def test_a_server_can_still_bind() -> None:
    out = run_guarded("""
        server = socket.socket()
        print(attempt(lambda: server.bind(("0.0.0.0", 0))))
        print(attempt(lambda: socket.getaddrinfo("0.0.0.0", 8080)))
    """)
    assert out.split() == ["allowed", "allowed"]


def test_unset_allowlist_installs_nothing() -> None:
    guard_egress(Settings())


@pytest.mark.parametrize(
    "settings",
    [
        Settings(collector_url="https://collector.example.test", egress_allowed_hosts="a.test"),
        Settings(egress_allowed_hosts="collector.example.test"),
    ],
)
def test_allowlist_must_include_the_collector(settings: Settings) -> None:
    with pytest.raises(ValueError, match="COLLECTOR_URL"):
        guard_egress(settings)
