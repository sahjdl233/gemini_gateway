"""Anonymous Vertex MVP acceptance (ANON-012 follow-up): the FULL chain
on the real production construction path with the real network.

Opt-in only: ``GEMINI_GATEWAY_REAL_ANON_VERTEX=1``.  Skipped otherwise.

Chain under test::

    NodeDefinition/NodeRuntimeState
        -> NodePool (AdmissionProjection, push+local-read)
        -> Admission (Connectivity / Capability / Auth, per-node egress)
        -> AdmissionScheduler (immediate first round)
        -> READY node enters the request pool
        -> 429/failure -> node-level cooldown (runtime)
        -> AnonymousVertexProvider.complete()/stream()  (REAL upstream)
        -> shutdown/lifecycle

Known environment fact (ANON-003): stock httpx is blocked at the
application layer by the upstream ("Failed to verify action"), so real
requests are expected to surface as a CLASSIFIED AuthenticationError.
This test asserts the chain mechanics, not Google's verdict.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GEMINI_GATEWAY_REAL_ANON_VERTEX") != "1",
    reason="real Google upstream; opt-in via GEMINI_GATEWAY_REAL_ANON_VERTEX=1",
)

from providers.anonymous_vertex.admission import NodeAdmissionState
from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionPipelineResult,
)
from providers.anonymous_vertex.admission import NodeAdmissionResult
from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory
from providers.anonymous_vertex.provider import AnonymousVertexProvider
from providers.anonymous_vertex.errors import AnonymousVertexAuthError

NODE_CONFIG = {
    "enabled": True,
    "node_pool": {
        "admission_interval_seconds": 0.2,
        "nodes": [
            {"id": "node-a", "max_concurrency": 2},
            {"id": "node-b", "max_concurrency": 2},
        ],
    },
}


def _pipeline(node_id, state, reason=None):
    final = NodeAdmissionResult(
        state=state, reason=reason,
        checked_at=datetime.now(timezone.utc),
    )
    return AdmissionPipelineResult(
        node_id=node_id, final_result=final, attempts=(final,)
    )


def _wait_round(provider, n, timeout=30.0):
    async def run():
        deadline = asyncio.get_event_loop().time() + timeout
        while provider.admission_scheduler.stats["rounds"] < n:
            if asyncio.get_event_loop().time() > deadline:
                raise asyncio.TimeoutError("round did not complete")
            await asyncio.sleep(0.05)
    return run


def _request():
    from core.models import ChatRequest, ChatMessage

    return ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="hello")],
    )


async def test_mvp_full_chain_real_upstream():
    from core.errors import ProviderError

    # ------------------------------------------------------------------
    # Phase 1: production wiring + REAL admission checkers (per-node
    # egress: direct here).  Whatever Google answers, the gate must
    # reflect it and the pool must obey.
    # ------------------------------------------------------------------
    provider = AnonymousVertexProviderFactory().create_provider(
        "anonymous_vertex", dict(NODE_CONFIG)
    )
    assert provider.admission_projection is not None
    # no admission result yet -> not schedulable (never default-READY)
    assert await provider.node_pool.acquire() is None

    await provider.start_admission()
    await _wait_round(provider, 1)()
    snap = provider.admission_projection.snapshot()
    health = await provider.admission_store.health_snapshot()
    print("\n[mvp] phase1 admission verdicts (REAL checkers):")
    for h in health:
        print(f"[mvp]   {h.node_id}: {h.state.value} reason={h.reason!r}")
    for node_id, state in snap.items():
        latest = await provider.admission_store.get(node_id)
        assert latest.final_result.state == state
    # gate obeys the projection exactly
    if all(s != NodeAdmissionState.READY for s in snap.values()):
        assert await provider.node_pool.acquire() is None
    await provider.close()
    assert not provider.admission_running

    # ------------------------------------------------------------------
    # Phase 2: production wiring + scripted READY admission (simulating a
    # passing admission), then REAL non-stream + stream requests through
    # the node's own transport + real reCAPTCHA + real upstream.
    # ------------------------------------------------------------------
    class ReadyOrchestrator:
        async def check(self, node):
            return _pipeline(node.node_id, NodeAdmissionState.READY)

    provider = AnonymousVertexProviderFactory().create_provider(
        "anonymous_vertex", dict(NODE_CONFIG)
    )
    provider._admission_orchestrator = ReadyOrchestrator()
    await provider.start_admission()
    try:
        await _wait_round(provider, 1)()
        assert provider.admission_projection.snapshot() == {
            "node-a": NodeAdmissionState.READY,
            "node-b": NodeAdmissionState.READY,
        }
        lease = await provider.node_pool.acquire()
        assert lease is not None and lease.node_id == "node-a"
        await provider.node_pool.release(lease)

        # ---- REAL non-stream request ----
        request = _request()
        t0 = asyncio.get_event_loop().time()
        try:
            response = await provider.complete(request, None)
            print(
                f"[mvp] non-stream OK in {asyncio.get_event_loop().time()-t0:.2f}s: "
                f"{response.text[:100]!r} usage={response.usage}"
            )
        except AnonymousVertexAuthError as exc:
            # the documented ANON-003 blocker: stock httpx is rejected at
            # the application layer — the error chain must classify it
            print(
                f"[mvp] non-stream classified auth failure in "
                f"{asyncio.get_event_loop().time()-t0:.2f}s: {exc}"
            )
        node_a = provider.node_pool.nodes[0]
        print(
            f"[mvp] node-a runtime after request: requests={node_a.total_requests} "
            f"failures={node_a.total_failures} recaptcha={node_a.recaptcha_state}"
        )
        assert node_a.total_requests >= 1  # outcome recorded on the node

        # ---- REAL streaming request ----
        chunks = []
        try:
            async for chunk in provider.stream(request, None):
                chunks.append(chunk)
            print(f"[mvp] stream OK: {len(chunks)} chunks")
        except ProviderError as exc:
            print(f"[mvp] stream classified failure: {type(exc).__name__}: {exc}")
        print(f"[mvp] stream chunk count: {len(chunks)}")

        # ---- 429/5xx style node-level cooldown mechanics (no real 429
        # needed; the runtime ladder is unit-tested) ----
        await provider.node_pool.record_rate_limit("node-a")
        assert node_a.cooldown_until > 0
        await provider.node_pool.record_success("node-a")
        assert node_a.cooldown_until == 0.0
    finally:
        await provider.close()
        assert not provider.admission_running

    print("[mvp] chain verification complete")
