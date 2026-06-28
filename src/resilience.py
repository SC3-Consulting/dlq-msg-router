"""
Resilience primitives: exponential backoff with jitter and a per-key circuit breaker.

Design principles:
- Backoff and circuit state are pure-Python with no external dependencies.
- The circuit breaker is keyed by an arbitrary string (e.g. queue name or 'ai'),
  allowing independent trip/recover per dependency.
- All thresholds are configurable via environment variables so Ops can tune
  without rebuilding the image.
"""

import logging
import math
import os
import random
import threading
import time
from typing import Optional

logger = logging.getLogger("Resilience")


# ---------------------------------------------------------------------------
# Exponential backoff helper
# ---------------------------------------------------------------------------


def backoff_sleep(
    attempt: int,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    jitter: bool = True,
) -> float:
    """
    Sleep for an exponentially increasing duration with optional full jitter.

    Returns the actual sleep duration (useful for logging / tests).

    Formula (full jitter):  sleep = random(0, min(cap, base * 2^attempt))
    Formula (no jitter):    sleep = min(cap, base * 2^attempt)

    Args:
        attempt (int): The current retry attempt (0-based).
        base_seconds (float): The base sleep duration in seconds (default: 1.0).
        max_seconds (float): The maximum sleep duration in seconds (default: 60.0).
        jitter (bool): Whether to apply full jitter (default: True).

    Returns:
        float: The actual sleep duration in seconds.
    """
    ceiling = min(max_seconds, base_seconds * math.pow(2, attempt))
    duration = random.uniform(0, ceiling) if jitter else ceiling
    if duration > 0:
        time.sleep(duration)
    return duration


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState:
    """Circuit breaker states."""

    CLOSED = "CLOSED"  # Normal operation
    OPEN = "OPEN"  # Tripped — fast-fail all calls
    HALF_OPEN = "HALF_OPEN"  # Probe: one trial call allowed


class CircuitBreakerOpenError(Exception):
    """Raised when a call is attempted against an OPEN circuit.
    Attributes:
        key (str): The key associated with the circuit breaker.
    """


class CircuitBreaker:
    """
    Per-key circuit breaker backed by a shared registry.

    Configuration (read once per instance from env, to allow per-test overrides):
      CIRCUIT_FAILURE_THRESHOLD  - consecutive failures to trip (default 5)
      CIRCUIT_RECOVERY_TIMEOUT   - seconds before attempting HALF_OPEN (default 60)
      CIRCUIT_HALF_OPEN_SUCCESSES - successes in HALF_OPEN before closing (default 1)

    Usage:
        cb = CircuitBreaker("queue-name")
        if cb.allow_request():
            try:
                # perform operation
                cb.record_success()
            except Exception:
                cb.record_failure()
    """

    _registry: dict = {}
    _registry_lock = threading.Lock()

    def __init__(self, key: str):
        """
        Initialises a CircuitBreaker instance for the specified key.
        Args:
            key (str): The unique key identifying the circuit breaker instance.
        """
        self.key = key
        self.failure_threshold = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "5"))
        self.recovery_timeout = float(os.getenv("CIRCUIT_RECOVERY_TIMEOUT", "60"))
        self.half_open_successes_needed = int(
            os.getenv("CIRCUIT_HALF_OPEN_SUCCESSES", "1")
        )

        with CircuitBreaker._registry_lock:
            if key not in CircuitBreaker._registry:
                CircuitBreaker._registry[key] = {
                    "state": CircuitState.CLOSED,
                    "failures": 0,
                    "half_open_successes": 0,
                    "opened_at": None,
                    "lock": threading.Lock(),
                }
        self._state = CircuitBreaker._registry[key]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Returns the current state of the circuit breaker (CLOSED, OPEN, HALF_OPEN).
        Returns:
            str: The current state of the circuit breaker.
        """
        return self._state["state"]

    def allow_request(self) -> bool:
        """Returns True if the call should proceed, False if it should be fast-failed.
        In HALF_OPEN state, allows exactly one probe call to test recovery.
        Returns:
            bool: True if the request is allowed, False otherwise.
        """
        with self._state["lock"]:
            if self._state["state"] == CircuitState.CLOSED:
                return True

            if self._state["state"] == CircuitState.OPEN:
                elapsed = time.time() - (self._state["opened_at"] or 0)
                if elapsed >= self.recovery_timeout:
                    self._transition(CircuitState.HALF_OPEN)
                    logger.info(
                        f"[CircuitBreaker:{self.key}] OPEN → HALF_OPEN after {elapsed:.1f}s"
                    )
                    return True
                return False

            # HALF_OPEN: allow exactly one probe
            return True

    def record_success(self) -> None:
        """Records a successful call, potentially closing the circuit if in HALF_OPEN state.
        In HALF_OPEN state, counts successes and transitions to CLOSED if enough successes are recorded.
        In CLOSED state, resets the failure count.
        """
        with self._state["lock"]:
            if self._state["state"] == CircuitState.HALF_OPEN:
                self._state["half_open_successes"] += 1
                if (
                    self._state["half_open_successes"]
                    >= self.half_open_successes_needed
                ):
                    self._transition(CircuitState.CLOSED)
                    logger.info(
                        f"[CircuitBreaker:{self.key}] HALF_OPEN → CLOSED (recovered)"
                    )
            elif self._state["state"] == CircuitState.CLOSED:
                self._state["failures"] = 0

    def record_failure(self) -> None:
        """Records a failed call, potentially opening the circuit if in HALF_OPEN or CLOSED state."""
        with self._state["lock"]:
            self._state["failures"] += 1
            if self._state["state"] == CircuitState.HALF_OPEN:
                # Probe failed — re-open immediately
                self._transition(CircuitState.OPEN)
                logger.warning(
                    f"[CircuitBreaker:{self.key}] HALF_OPEN → OPEN (probe failed)"
                )
            elif self._state["state"] == CircuitState.CLOSED:
                if self._state["failures"] >= self.failure_threshold:
                    self._transition(CircuitState.OPEN)
                    logger.warning(
                        f"[CircuitBreaker:{self.key}] CLOSED → OPEN after "
                        f"{self._state['failures']} consecutive failures"
                    )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _transition(self, new_state: str) -> None:
        """Transitions the circuit breaker to a new state.
        Must be called with self._state['lock'] held.
        Args:
            new_state (str): The new state to transition to (CLOSED, OPEN, HALF_OPEN).
        """
        self._state["state"] = new_state
        if new_state == CircuitState.OPEN:
            self._state["opened_at"] = time.time()
        elif new_state == CircuitState.CLOSED:
            self._state["failures"] = 0
            self._state["half_open_successes"] = 0
            self._state["opened_at"] = None
        elif new_state == CircuitState.HALF_OPEN:
            self._state["half_open_successes"] = 0

    @classmethod
    def reset(cls, key: Optional[str] = None) -> None:
        """Reset circuit state. Provide a key to reset one circuit, omit to reset all.
        Args:
            key (Optional[str]): The key of the circuit to reset. If None, all circuits are reset.
        """
        with cls._registry_lock:
            if key is not None:
                cls._registry.pop(key, None)
            else:
                cls._registry.clear()
