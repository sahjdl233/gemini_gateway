"""Cooldown management.

429 must be a first-class citizen: Retry-After when upstream provides it,
otherwise exponential backoff + jitter.  Never hardcode a sleep(60).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Callable, Optional

from .health import HealthState
from .resource import Resource, utcnow

NowFn = Callable[[], datetime]


def _default_now() -> datetime:
    return utcnow()


class CooldownManager:
    def __init__(
        self,
        *,
        base_delay: float = 0.2,
        factor: float = 2.0,
        max_delay: float = 10.0,
        jitter: float = 0.1,
        degrade_threshold: int = 3,
        now_fn: Optional[NowFn] = None,
    ) -> None:
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.degrade_threshold = degrade_threshold
        self._now_fn = now_fn or _default_now

    def now(self) -> datetime:
        return self._now_fn()

    def _delay_seconds(self, resource: Resource, retry_after: Optional[float]) -> float:
        """Retry-After wins; otherwise exponential backoff + jitter."""
        if retry_after is not None and retry_after > 0:
            base = float(retry_after)
        else:
            attempts = max(resource.consecutive_failures, 1) - 1
            base = min(self.max_delay, self.base_delay * (self.factor**attempts))
        return base * (1.0 + random.uniform(0.0, self.jitter))

    def in_cooldown(self, resource: Resource) -> bool:
        """True if the resource is cooling down right now.

        Also auto-recovers a resource whose cooldown has expired.
        """
        if resource.cooldown_until is None:
            return False
        now = self.now()
        if now >= resource.cooldown_until:
            resource.cooldown_until = None
            resource.health = (
                HealthState.DEGRADED
                if resource.consecutive_failures >= self.degrade_threshold
                else HealthState.HEALTHY
            )
            return False
        return True

    def apply_rate_limit(
        self, resource: Resource, retry_after: Optional[float] = None
    ) -> None:
        """429 -> COOLDOWN (uses Retry-After or backoff)."""
        resource.consecutive_failures += 1
        delay = self._delay_seconds(resource, retry_after)
        resource.cooldown_until = self.now() + timedelta(seconds=delay)
        resource.health = HealthState.COOLDOWN

    def apply_failure(self, resource: Resource, retry_after: Optional[float] = None) -> None:
        """Non-rate-limit failure -> DEGRADED, COOLDOWN once too many."""
        resource.consecutive_failures += 1
        if retry_after is not None and retry_after > 0:
            delay = float(retry_after) * (1.0 + random.uniform(0.0, self.jitter))
            resource.cooldown_until = self.now() + timedelta(seconds=delay)
            resource.health = HealthState.COOLDOWN
        elif resource.consecutive_failures >= self.degrade_threshold:
            delay = self._delay_seconds(resource, None)
            resource.cooldown_until = self.now() + timedelta(seconds=delay)
            resource.health = HealthState.COOLDOWN
        else:
            resource.health = HealthState.DEGRADED

    def reset(self, resource: Resource) -> None:
        resource.consecutive_failures = 0
        resource.cooldown_until = None
        resource.health = HealthState.HEALTHY
