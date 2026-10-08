"""In-process egress allowlist, for a sensor on a host with no network-level egress rule.

On the compose deployment the sensor sits on an internal Docker network whose only way
out is a Squid proxy (infra/compose.support.yml). A platform such as Render offers no
such network, so for week one (docs/no-card-launch.md) this guard keeps the sensor
process to the same destinations: name lookups only for the allowed host names, and
connections only to addresses those lookups returned, on port 443.

It is an audit hook, which Python code cannot remove once installed. Native code in the
process could still bypass it, which the network rule would not allow; that is the
accepted week-one gap.
"""

import socket
import sys
from typing import Any

HTTPS_PORT = 443
# Names a server looks up to bind, not to connect: unspecified addresses and no name.
_BIND_NAMES = frozenset({None, "", "0.0.0.0", "::"})
_LOOKUP_EVENTS = frozenset(
    {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}
)
_SEND_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})


class EgressBlocked(PermissionError):
    """The sensor tried to reach a destination outside its egress allowlist."""


def _host_name(host: object) -> str | None:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None:
        return None
    return str(host).lower().rstrip(".")


def install_egress_guard(allowed_hosts: frozenset[str]) -> None:
    """Allow lookups of allowed_hosts and connections to what they resolve to, port 443.

    Process-wide and permanent: call it once, at sensor start-up, before any connection.
    """
    allowed = frozenset(name for host in allowed_hosts if (name := _host_name(host)))
    resolved: set[str] = set()
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(
        host: bytes | str | None,
        port: bytes | str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[Any]:
        results = real_getaddrinfo(host, port, family, type, proto, flags)
        if _host_name(host) in allowed:
            resolved.update(str(sockaddr[0]) for *_, sockaddr in results)
        return results

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event in _LOOKUP_EVENTS:
            name = _host_name(args[0])
            if name not in allowed and name not in _BIND_NAMES:
                raise EgressBlocked(f"egress: lookup of {name!r} is not allowed")
        elif event in _SEND_EVENTS:
            address = args[1]
            # None: a connected socket's sendmsg. A str or bytes path: a Unix socket.
            if not isinstance(address, tuple):
                return
            host, port = str(address[0]), address[1]
            if host not in resolved or port != HTTPS_PORT:
                raise EgressBlocked(f"egress: connection to {host}:{port} is not allowed")

    sys.addaudithook(hook)
    socket.getaddrinfo = getaddrinfo
