import os

from .breaker import WINDOW_SIZE as BREAKER_WINDOW_SIZE

SERVICE_B_URL = os.environ.get("SERVICE_B_URL", "http://service-b:8000/work")

# Per-attempt timeout for the call to Service B.
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "2.0"))

# none: single attempt, no retry.
# immediate: up to MAX_ATTEMPTS-1 retries, fired right after each failure.
# backoff: retries delayed by a random value in [RETRY_BACKOFF_MIN_MS, RETRY_BACKOFF_MAX_MS].
# full_jitter: retries delayed by random(0, RETRY_BASE_DELAYS_MS[attempt-1]) --
#   exponential backoff base with full jitter (Experiment 004).
RETRY_POLICY = os.environ.get("RETRY_POLICY", "none")
RETRY_BACKOFF_MIN_MS = float(os.environ.get("RETRY_BACKOFF_MIN_MS", "50"))
RETRY_BACKOFF_MAX_MS = float(os.environ.get("RETRY_BACKOFF_MAX_MS", "100"))

# Base delay (ms) for full_jitter, indexed by attempt-1 (attempt 1 = first
# retry, attempt 2 = second retry). Fixed, not swept -- see Experiment 004
# design decision 2.
RETRY_BASE_DELAYS_MS = [100.0, 200.0]

# Experiment 004 deliberately raises this from 2 (Experiments 002/003) to 3:
# a single retry only compares delay lengths, not a scheduling strategy --
# testing whether jitter desynchronizes retries requires multiple retry
# waves to actually exercise the mechanism.
MAX_ATTEMPTS = 1 if RETRY_POLICY == "none" else 3

BREAKER_ENABLED = os.environ.get("BREAKER_ENABLED", "false").lower() == "true"


def client_config() -> dict:
    return {
        "service_b_url": SERVICE_B_URL,
        "http_timeout": HTTP_TIMEOUT,
        "retry_policy": RETRY_POLICY,
        "retry_backoff_min_ms": RETRY_BACKOFF_MIN_MS,
        "retry_backoff_max_ms": RETRY_BACKOFF_MAX_MS,
        "max_attempts": MAX_ATTEMPTS,
        "breaker_enabled": BREAKER_ENABLED,
        "breaker_window_size": BREAKER_WINDOW_SIZE,
    }
