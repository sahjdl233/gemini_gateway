"""ANON-008-C acceptance tests: failure classification & quarantine policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.admission_policy import (
    AdmissionPolicy,
    DefaultFailureClassifier,
    FailureCategory,
    FailureClassifier,
)

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
CLASSIFIER = DefaultFailureClassifier()
POLICY = AdmissionPolicy()


def _failed(reason, state=NodeAdmissionState.FAILED):
    return NodeAdmissionResult(state=state, reason=reason, checked_at=NOW)


# ---------------------------------------------------------------------------
# FailureClassifier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reason", [
    "timeout", "network_error", "rate_limited", "server_busy",
    "connection refused", "temporarily unavailable",
])
def test_transient_reasons(reason):
    assert CLASSIFIER.classify(_failed(reason)) == FailureCategory.TRANSIENT


@pytest.mark.parametrize("reason", [
    "invalid_token", "expired_token", "unauthorized", "auth_failed",
    "credential rejected",
])
def test_auth_reasons(reason):
    assert CLASSIFIER.classify(_failed(reason)) == FailureCategory.AUTH


@pytest.mark.parametrize("reason", [
    "captcha_required", "recaptcha", "challenge_required", "risk_control",
])
def test_captcha_reasons(reason):
    assert CLASSIFIER.classify(_failed(reason)) == FailureCategory.CAPTCHA


@pytest.mark.parametrize("reason", ["blocked", "forbidden", "access denied"])
def test_blocked_reasons(reason):
    assert CLASSIFIER.classify(_failed(reason)) == FailureCategory.BLOCKED


@pytest.mark.parametrize("reason", [
    "invalid_endpoint", "malformed_response", "bad_request",
])
def test_invalid_reasons(reason):
    assert CLASSIFIER.classify(_failed(reason)) == FailureCategory.INVALID


def test_unknown_and_empty_reasons():
    assert CLASSIFIER.classify(_failed("something weird")) == FailureCategory.UNKNOWN
    assert CLASSIFIER.classify(_failed(None)) == FailureCategory.UNKNOWN
    # case-insensitive matching
    assert CLASSIFIER.classify(_failed("TIMEOUT")) == FailureCategory.TRANSIENT


def test_ready_maps_to_none():
    ready = NodeAdmissionResult(
        state=NodeAdmissionState.READY, reason=None, checked_at=NOW
    )
    assert CLASSIFIER.classify(ready) == FailureCategory.NONE


def test_keyword_precedence_invalid_token_is_auth_not_invalid():
    """Overlapping substrings resolve by rule order: AUTH wins over
    INVALID for 'invalid_token'."""
    assert CLASSIFIER.classify(_failed("invalid_token")) == FailureCategory.AUTH


def test_classifier_is_protocol_conforming_and_pure():
    assert isinstance(CLASSIFIER, FailureClassifier)
    result = _failed("timeout")
    CLASSIFIER.classify(result)
    assert result.state == NodeAdmissionState.FAILED  # result untouched
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# AdmissionPolicy
# ---------------------------------------------------------------------------
def test_ready_stays_ready_unchanged():
    ready = NodeAdmissionResult(
        state=NodeAdmissionState.READY, reason=None, checked_at=NOW
    )
    outcome = POLICY.decide(ready)
    assert outcome.state == NodeAdmissionState.READY
    assert outcome is ready  # identity preserved, nothing derived


def test_transient_failure_stays_failed():
    outcome = POLICY.decide(_failed("timeout"))
    assert outcome.state == NodeAdmissionState.FAILED
    assert outcome.reason == "timeout"
    assert outcome.checked_at == NOW


def test_unknown_category_failure_stays_failed():
    outcome = POLICY.decide(_failed("something weird"))
    assert outcome.state == NodeAdmissionState.FAILED


@pytest.mark.parametrize("reason", [
    "captcha_required", "invalid_token", "blocked", "invalid_endpoint",
])
def test_hard_failures_become_quarantined(reason):
    outcome = POLICY.decide(_failed(reason))
    assert outcome.state == NodeAdmissionState.QUARANTINED


def test_reason_is_preserved_on_quarantine():
    outcome = POLICY.decide(_failed("captcha_required"))
    assert outcome.state == NodeAdmissionState.QUARANTINED
    assert outcome.reason == "captcha_required"
    assert outcome.checked_at == NOW  # same check, derived state only


def test_policy_does_not_mutate_input():
    original = _failed("captcha_required")
    outcome = POLICY.decide(original)
    assert original.state == NodeAdmissionState.FAILED  # unchanged
    assert original.reason == "captcha_required"
    assert outcome is not original  # a new immutable result was derived


def test_non_policy_input_states_pass_through():
    for state in (
        NodeAdmissionState.TESTING,
        NodeAdmissionState.QUARANTINED,
        NodeAdmissionState.UNKNOWN,
    ):
        result = NodeAdmissionResult(state=state, reason="x", checked_at=NOW)
        assert POLICY.decide(result) is result


def test_policy_accepts_custom_classifier():
    class AlwaysCaptcha:
        def classify(self, result):
            return FailureCategory.CAPTCHA

    policy = AdmissionPolicy(classifier=AlwaysCaptcha())
    assert isinstance(policy.classifier, FailureClassifier)
    assert POLICY.decide(_failed("timeout")).state == NodeAdmissionState.FAILED
    outcome = policy.decide(_failed("timeout"))
    assert outcome.state == NodeAdmissionState.QUARANTINED
    assert outcome.reason == "timeout"


def test_policy_outputs_respect_transition_matrix():
    """Every policy outcome is a legal admission transition from FAILED."""
    from providers.anonymous_vertex.admission import can_transition

    for reason in ("timeout", "captcha_required", "invalid_token", "blocked"):
        outcome = POLICY.decide(_failed(reason))
        # staying FAILED is not a transition (legal no-op); any state CHANGE
        # must be a legal edge of the ANON-008-A matrix
        assert (
            outcome.state == NodeAdmissionState.FAILED
            or can_transition(NodeAdmissionState.FAILED, outcome.state)
        )
