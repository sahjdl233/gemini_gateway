"""Admission failure classification and quarantine policy (ANON-008-C).

The final domain link of the admission chain::

    Checker -> Result -> Classifier -> Policy -> READY / FAILED / QUARANTINED

Two cooperating pieces, both pure and synchronous (no network, no I/O):

* :class:`FailureClassifier` derives a :class:`FailureCategory` from a
  :class:`NodeAdmissionResult` — string-level classification only;
* :class:`AdmissionPolicy` maps a category to the node's next admission
  state: transient/unknown failures stay FAILED (re-checkable), while
  auth / captcha / blocked / invalid failures are QUARANTINED.

Boundary rule: ``NodeAdmissionResult`` is NEVER extended with a category
field — the result is the checker's output, the category is a policy
derivation.  The policy returns NEW immutable results (or the original
object untouched); it never mutates anything.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)

__all__ = [
    "FailureCategory",
    "FailureClassifier",
    "DefaultFailureClassifier",
    "AdmissionPolicy",
]


class FailureCategory(str, Enum):
    """Semantic class of an admission failure (drives quarantine policy)."""

    NONE = "none"          # success (READY)
    TRANSIENT = "transient"  # timeout / network / busy / rate-limited
    AUTH = "auth"            # token / credential / unauthorized
    CAPTCHA = "captcha"      # captcha / risk-control challenges
    BLOCKED = "blocked"      # blocked / forbidden / denied
    INVALID = "invalid"      # endpoint or payload configuration errors
    UNKNOWN = "unknown"      # unclassifiable


#: Keyword rules, evaluated IN ORDER — earlier categories win over later
#: ones on overlapping substrings (e.g. ``invalid_token`` is AUTH, not
#: INVALID).  Lowercase substring match against ``result.reason``.
_CATEGORY_KEYWORDS: tuple = (
    (FailureCategory.TRANSIENT, (
        "timeout", "network", "connection", "temporarily", "rate_limit",
        "busy",
    )),
    (FailureCategory.AUTH, (
        "auth", "token", "credential", "unauthorized",
    )),
    (FailureCategory.CAPTCHA, (
        "captcha", "recaptcha", "challenge", "risk",
    )),
    (FailureCategory.BLOCKED, (
        "blocked", "forbidden", "denied",
    )),
    (FailureCategory.INVALID, (
        "invalid", "malformed", "bad_request",
    )),
)


@runtime_checkable
class FailureClassifier(Protocol):
    """Derives a :class:`FailureCategory` from an admission result.

    Synchronous, side-effect free: no network access and no mutation of
    the result.
    """

    def classify(self, result: NodeAdmissionResult) -> FailureCategory:
        ...


class DefaultFailureClassifier:
    """String-level keyword classifier (no real protocol knowledge)."""

    def classify(self, result: NodeAdmissionResult) -> FailureCategory:
        if result.state == NodeAdmissionState.READY:
            return FailureCategory.NONE
        reason = (result.reason or "").lower()
        if not reason:
            return FailureCategory.UNKNOWN
        for category, keywords in _CATEGORY_KEYWORDS:
            if any(keyword in reason for keyword in keywords):
                return category
        return FailureCategory.UNKNOWN


class AdmissionPolicy:
    """Maps a classification onto the node's next admission state.

    READY stays READY; FAILED stays FAILED for TRANSIENT / UNKNOWN
    categories (the node remains re-checkable) and becomes QUARANTINED
    for AUTH / CAPTCHA / BLOCKED / INVALID.  The failure reason — and the
    original ``checked_at`` — are preserved on derived results.
    """

    _QUARANTINE_CATEGORIES = frozenset({
        FailureCategory.AUTH,
        FailureCategory.CAPTCHA,
        FailureCategory.BLOCKED,
        FailureCategory.INVALID,
    })

    def __init__(
        self, classifier: FailureClassifier | None = None
    ) -> None:
        self._classifier = classifier or DefaultFailureClassifier()

    @property
    def classifier(self) -> FailureClassifier:
        return self._classifier

    def decide(self, result: NodeAdmissionResult) -> NodeAdmissionResult:
        """Return the policy outcome for ``result`` (input never mutated)."""
        if result.state == NodeAdmissionState.READY:
            return result
        if result.state != NodeAdmissionState.FAILED:
            # TESTING / QUARANTINED / UNKNOWN inputs are not policy input;
            # pass them through untouched.
            return result

        category = self._classifier.classify(result)
        if category in self._QUARANTINE_CATEGORIES:
            return NodeAdmissionResult(
                state=NodeAdmissionState.QUARANTINED,
                reason=result.reason,
                checked_at=result.checked_at,
            )
        # TRANSIENT / UNKNOWN: stay FAILED, re-checkable later.
        return result
