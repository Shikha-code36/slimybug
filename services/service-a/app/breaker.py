import asyncio
import os
import time

WINDOW_SIZE = int(os.environ.get("BREAKER_WINDOW_SIZE", "20"))
FAILURE_RATE_THRESHOLD = 0.5
COOLDOWN_SECONDS = 2.0


class CircuitBreaker:
    """Closed / Open / Half-open, wrapping exactly one call (A -> B).

    Trip condition is a sliding failure-rate window over *forwarded*
    requests only -- a request that's short-circuited while already open
    must never feed back into this window, or the breaker would measure
    its own fail-fast output instead of Service B's real health and could
    never legitimately re-close except via the half-open probe.
    """

    def __init__(self):
        self.state = "closed"
        self._outcomes: list[bool] = []
        self._opened_at: float | None = None
        self._episode_start: float | None = None
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

        self.open_count = 0
        self.open_seconds_total = 0.0
        self.short_circuit_count = 0
        self.probe_count = 0
        self.probe_success_count = 0

    async def acquire(self) -> bool:
        """True if this request may be forwarded to Service B."""
        async with self._lock:
            now = time.monotonic()

            if self.state == "closed":
                return True

            if self.state == "open":
                if now - self._opened_at >= COOLDOWN_SECONDS:
                    self.state = "half_open"
                    self._probe_in_flight = True
                    self.probe_count += 1
                    return True
                self.short_circuit_count += 1
                return False

            # half_open: exactly one probe in flight at a time
            if not self._probe_in_flight:
                self._probe_in_flight = True
                self.probe_count += 1
                return True
            self.short_circuit_count += 1
            return False

    async def record(self, success: bool) -> None:
        """Report the outcome of a request that acquire() allowed through."""
        async with self._lock:
            if self.state == "half_open":
                self._probe_in_flight = False
                if success:
                    self.probe_success_count += 1
                    self._close()
                else:
                    self._open()
                return

            # closed: update the sliding window
            self._outcomes.append(success)
            if len(self._outcomes) > WINDOW_SIZE:
                self._outcomes.pop(0)
            if len(self._outcomes) == WINDOW_SIZE:
                failure_rate = 1 - (sum(self._outcomes) / WINDOW_SIZE)
                if failure_rate >= FAILURE_RATE_THRESHOLD:
                    self._open()

    def _open(self) -> None:
        now = time.monotonic()
        if self._episode_start is None:
            self._episode_start = now
            self.open_count += 1
        self.state = "open"
        self._opened_at = now
        self._outcomes.clear()
        self._probe_in_flight = False

    def _close(self) -> None:
        now = time.monotonic()
        if self._episode_start is not None:
            self.open_seconds_total += now - self._episode_start
        self.state = "closed"
        self._episode_start = None
        self._opened_at = None
        self._outcomes.clear()
        self._probe_in_flight = False

    def snapshot(self) -> dict:
        now = time.monotonic()
        open_seconds = self.open_seconds_total
        if self._episode_start is not None:
            open_seconds += now - self._episode_start
        return {
            "state": self.state,
            "open_count": self.open_count,
            "open_seconds_total": round(open_seconds, 3),
            "short_circuit_count": self.short_circuit_count,
            "probe_count": self.probe_count,
            "probe_success_count": self.probe_success_count,
        }
