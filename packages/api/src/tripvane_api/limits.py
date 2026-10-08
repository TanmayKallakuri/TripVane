"""Daily request limits for the lookup API, counted in memory per UTC day.

Counters live in the process, so the API runs as one uvicorn worker, and a restart
starts the day's counts again from zero.
"""

import ipaddress
import threading
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from tripvane_api.client import IPAddress

PUBLIC_DAILY_LIMIT = 50
KEY_DAILY_LIMIT = 5_000


class DailyLimiter:
    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._day: date | None = None
        self._counts: Counter[str] = Counter()

    def allow(self, client: str, limit: int) -> bool:
        """Count one request for client; False once it has made limit requests today."""
        with self._lock:
            today = self._now().date()
            if today != self._day:
                self._day = today
                self._counts.clear()
            if self._counts[client] >= limit:
                return False
            self._counts[client] += 1
            return True

    def seconds_until_reset(self) -> int:
        now = self._now()
        midnight = datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=now.tzinfo)
        return max(1, int((midnight - now).total_seconds()))


def ip_client(ip: IPAddress) -> str:
    """The limiter key for an address: IPv4 as is, IPv6 by its /64 network.

    One IPv6 host is normally given a whole /64, so counting single addresses would let
    one client rotate through billions of them.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        return f"ip:{ipaddress.IPv6Network((ip, 64), strict=False)}"
    return f"ip:{ip}"


def key_client(key_id: int) -> str:
    return f"key:{key_id}"
