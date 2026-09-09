"""Shared fixtures: fake clock, cooldown, pool/scheduler builders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from core.cooldown import CooldownManager
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from providers.fake import FakeProvider, FakeResource


class FakeClock:
    def __init__(self, start: Optional[datetime] = None) -> None:
        self.now = start or datetime.now(timezone.utc)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


def make_cooldown(clock: FakeClock, **kwargs) -> CooldownManager:
    return CooldownManager(now_fn=lambda: clock.now, **kwargs)


def make_resources(specs: List[dict]) -> List[FakeResource]:
    resources = []
    for spec in specs:
        resources.append(FakeResource.model_validate({
            "id": spec["id"],
            "provider": "fake",
            "scenario": spec.get("scenario", "success"),
            "retry_after": spec.get("retry_after"),
            "reply_text": spec.get("reply_text", "Hello from FakeProvider!"),
        }))
    return resources


def make_pool(clock: FakeClock, resources: List[FakeResource]) -> InMemoryPool:
    cooldown = make_cooldown(clock)
    return InMemoryPool(provider="fake", resources=resources, cooldown=cooldown)


def make_scheduler(
    clock: FakeClock, resources: List[FakeResource], max_retries: int = 2
) -> Scheduler:
    cooldown = make_cooldown(clock)
    provider = FakeProvider()
    pool = InMemoryPool(provider="fake", resources=resources, cooldown=cooldown)
    return Scheduler(
        providers={"fake": provider}, pools={"fake": pool}, max_retries=max_retries
    )


def make_chat_request(model: str = "gemini-3.8-flash", message: str = "hi") -> ChatRequest:
    from core.models import ChatMessage, ChatRequest

    return ChatRequest(
        model=model, messages=[ChatMessage(role="user", content=message)]
    )
