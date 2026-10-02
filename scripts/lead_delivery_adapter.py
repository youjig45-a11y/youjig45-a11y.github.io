"""Transportless adapter prototype. Injected fixtures do not establish authority."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import wraps
import json
from pathlib import Path
import time
from typing import Callable

from lead_delivery_state import (
    AlreadyComplete, Attempt, Busy, Claim, DeliveryStore, OutcomeUncertain,
    Receipt, Reconciliation, ReconciliationDenied, STAGES, StaleClaim, StateError, digest,
)


class AdapterError(Exception):
    """Only fixed categories are raised; candidate values are never formatted."""


def _fail(category: str):
    raise AdapterError(category) from None


_SAFE_ERROR_CATEGORIES = {
    AdapterError: frozenset({
        "candidate_fields_invalid", "candidate_json_invalid", "candidate_scope_invalid",
        "candidate_receipt_invalid", "candidate_digest_invalid", "candidate_version_unsupported",
        "candidate_decision_invalid", "candidate_drain_evidence_required",
        "receipt_verifier_missing", "private_reference_store_missing", "operator_authorization_required",
        "private_reference_invalid", "adapter_unavailable", "receipt_verification_denied",
        "private_reference_unavailable", "candidate_scope_mismatch", "confirmed_reference_missing",
        "effect_result_invalid", "effect_confirmation_uncertain", "reconciliation_busy",
        "adapter_initialization_failed", "adapter_close_failed", "adapter_resolution_failed",
        "adapter_advance_failed", "adapter_reconciliation_failed",
    }),
    StateError: frozenset({
        "invalid_reference", "invalid_identifier", "invalid_digest",
        "explicit_absolute_store_path_required", "invalid_clock", "unsupported_store_schema",
        "unknown_lead", "invalid_lease", "invalid_receipt", "invalid_reconciliation",
        "reconciliation_stale",
    }),
    Busy: frozenset({"claim_busy"}),
    StaleClaim: frozenset({"claim_stale", "attempt_stale"}),
    OutcomeUncertain: frozenset({"reconciliation_required"}),
    AlreadyComplete: frozenset({"delivery_already_complete"}),
    ReconciliationDenied: frozenset({"operator_authorization_required"}),
}


def _public_boundary(category):
    """Preserve only exact known semantic errors; suppress private error chains."""
    def decorate(method):
        @wraps(method)
        def checked(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except Exception as error:
                allowed = _SAFE_ERROR_CATEGORIES.get(type(error))
                if (allowed is not None and len(error.args) == 1
                        and type(error.args[0]) is str and error.args[0] in allowed):
                    raise type(error)(error.args[0]) from None
                _fail(category)
        return checked
    return decorate


def _hex(value, length=64):
    return (type(value) is str and len(value) == length
            and all(c in "0123456789abcdef" for c in value))


def _fields(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        _fail("candidate_fields_invalid")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("candidate_fields_invalid")
        result[key] = value
    return result


def _json(raw):
    if type(raw) is not str or len(raw) > 16384:
        _fail("candidate_json_invalid")
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except Exception:
        _fail("candidate_json_invalid")


@dataclass(frozen=True, repr=False)
class Scope:
    key: str
    stage: str
    attempt_token: str
    generation: int


def _scope(value):
    _fields(value, ("key", "stage", "attempt_token", "generation"))
    scope = Scope(**value)
    _validate_scope(scope)
    return scope


def _validate_scope(scope):
    if (type(scope) is not Scope or not _hex(scope.key)
            or type(scope.stage) is not str or scope.stage not in STAGES
            or not _hex(scope.attempt_token, 32)
            or type(scope.generation) is not int or scope.generation <= 0):
        _fail("candidate_scope_invalid")


def _attempt_scope(attempt):
    scope = Scope(attempt.key, attempt.stage, attempt.token, attempt.generation)
    _validate_scope(scope)
    return scope


@dataclass(frozen=True, repr=False)
class ReceiptCandidate:
    scope: Scope
    evidence_digest: str
    effect_digest: str


def _validate_receipt_candidate(candidate):
    if type(candidate) is not ReceiptCandidate:
        _fail("candidate_receipt_invalid")
    _validate_scope(candidate.scope)
    if not _hex(candidate.evidence_digest) or not _hex(candidate.effect_digest):
        _fail("candidate_digest_invalid")


def _receipt(value):
    _fields(value, ("version", "scope", "evidence_digest", "effect_digest"))
    if type(value["version"]) is not int or value["version"] != 1:
        _fail("candidate_version_unsupported")
    candidate = ReceiptCandidate(_scope(value["scope"]),
                                 value["evidence_digest"], value["effect_digest"])
    _validate_receipt_candidate(candidate)
    return candidate


def parse_receipt_candidate(raw: str) -> ReceiptCandidate:
    return _receipt(_json(raw))


@dataclass(frozen=True, repr=False)
class ReconciliationCandidate:
    scope: Scope
    decision: str
    authority_digest: str
    evidence_digest: str
    drain_evidence_digest: str | None
    receipt: ReceiptCandidate | None


def parse_reconciliation_candidate(raw: str) -> ReconciliationCandidate:
    value = _json(raw)
    _fields(value, ("version", "scope", "decision", "authority_digest",
                    "evidence_digest", "drain_evidence_digest", "receipt"))
    if type(value["version"]) is not int or value["version"] != 1:
        _fail("candidate_version_unsupported")
    scope = _scope(value["scope"])
    decision = value["decision"]
    if type(decision) is not str or decision not in ("confirmed", "no_effect"):
        _fail("candidate_decision_invalid")
    if not _hex(value["authority_digest"]) or not _hex(value["evidence_digest"]):
        _fail("candidate_digest_invalid")
    if decision == "confirmed":
        receipt = _receipt(value["receipt"])
        if receipt.scope != scope or value["drain_evidence_digest"] is not None:
            _fail("candidate_scope_invalid")
    else:
        receipt = None
        if value["receipt"] is not None or not _hex(value["drain_evidence_digest"]):
            _fail("candidate_drain_evidence_required")
    return ReconciliationCandidate(scope, decision, value["authority_digest"],
                                   value["evidence_digest"],
                                   value["drain_evidence_digest"], receipt)


@dataclass(frozen=True, repr=False)
class PrivateEffectReference:
    scope: Scope
    value: str


@dataclass(frozen=True, repr=False)
class VerifiedReceipt:
    scope: Scope
    evidence_digest: str
    effect_digest: str


@dataclass(frozen=True, repr=False)
class VerifiedAuthority:
    scope: Scope
    decision: str
    authority_digest: str
    evidence_digest: str
    drain_evidence_digest: str | None


@dataclass(frozen=True, repr=False)
class EffectResult:
    candidate_json: str
    reference: PrivateEffectReference


class ReceiptVerifier(ABC):
    @abstractmethod
    def require_available(self) -> None:
        """Default denial before beginning an effect; availability is not proof."""

    @abstractmethod
    def verify(self, candidate: ReceiptCandidate,
               reference: PrivateEffectReference) -> VerifiedReceipt:
        """Authenticate receipt/readback outside this prototype."""


class PrivateReferenceStore(ABC):
    @abstractmethod
    def require_available(self) -> None:
        """Require an explicitly provided private reference implementation."""

    @abstractmethod
    def persist(self, reference: PrivateEffectReference) -> None:
        """Persist an immutable attempt-scoped reference before confirmation."""

    @abstractmethod
    def resolve(self, scope: Scope, effect_digest: str) -> PrivateEffectReference:
        """Return exact private identity; never infer it from a digest."""


class AuthorityVerifier(ABC):
    @abstractmethod
    def verify(self, candidate: ReconciliationCandidate) -> VerifiedAuthority:
        """Authenticate exact authority, readback and, for no_effect, drain proof."""


class DeniedReceiptVerifier(ReceiptVerifier):
    def require_available(self):
        _fail("receipt_verifier_missing")

    def verify(self, candidate, reference):
        _fail("receipt_verifier_missing")


class DeniedPrivateReferenceStore(PrivateReferenceStore):
    def require_available(self):
        _fail("private_reference_store_missing")

    def persist(self, reference):
        _fail("private_reference_store_missing")

    def resolve(self, scope, effect_digest):
        _fail("private_reference_store_missing")


class DeniedAuthorityVerifier(AuthorityVerifier):
    def verify(self, candidate):
        _fail("operator_authorization_required")


def _reference(reference, scope, effect_digest):
    if type(reference) is not PrivateEffectReference:
        _fail("private_reference_invalid")
    _validate_scope(reference.scope)
    if (reference.scope != scope
            or type(reference.value) is not str or not reference.value.strip()
            or len(reference.value) > 4096 or digest(reference.value) != effect_digest):
        _fail("private_reference_invalid")
    return reference


class DeliveryAdapter:
    """Caller-specified local engine; no HTTP, default path or production entry."""

    @_public_boundary("adapter_initialization_failed")
    def __init__(self, path: Path, *, references: PrivateReferenceStore | None = None,
                 receipts: ReceiptVerifier | None = None,
                 authority: AuthorityVerifier | None = None,
                 clock: Callable[[], float] = time.time):
        self.references = references if references is not None else DeniedPrivateReferenceStore()
        self.receipts = receipts if receipts is not None else DeniedReceiptVerifier()
        self.authority = authority if authority is not None else DeniedAuthorityVerifier()
        self._authorized_request = None
        self.store = DeliveryStore(path, clock=clock, authorizer=self._authorize)

    def _authorize(self, request):
        # The permit exists only during one verified reconciliation call. This
        # trusted-code injection boundary is not an operator authentication system.
        return self._authorized_request is not None and request is self._authorized_request

    @_public_boundary("adapter_close_failed")
    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _require_available(self):
        try:
            if (self.receipts.require_available() is not None
                    or self.references.require_available() is not None):
                _fail("adapter_unavailable")
        except Exception:
            _fail("adapter_unavailable")

    def _verify_receipt(self, candidate, reference):
        _validate_receipt_candidate(candidate)
        try:
            result = self.receipts.verify(candidate, reference)
        except Exception:
            _fail("receipt_verification_denied")
        if type(result) is not VerifiedReceipt:
            _fail("receipt_verification_denied")
        _validate_scope(result.scope)
        if (not _hex(result.evidence_digest) or not _hex(result.effect_digest)
                or result.scope != candidate.scope
                or result.evidence_digest != candidate.evidence_digest
                or result.effect_digest != candidate.effect_digest):
            _fail("receipt_verification_denied")
        return Receipt(candidate.scope.stage, result.evidence_digest, result.effect_digest)

    def _resolve(self, candidate):
        _validate_receipt_candidate(candidate)
        try:
            reference = self.references.resolve(candidate.scope, candidate.effect_digest)
            return _reference(reference, candidate.scope, candidate.effect_digest)
        except Exception:
            _fail("private_reference_unavailable")

    def _persisted_scope(self, scope):
        _validate_scope(scope)
        row = self.store.db.execute(
            "SELECT key,stage,generation FROM attempts WHERE token=?", (scope.attempt_token,),
        ).fetchone()
        if row is None or Scope(row["key"], row["stage"], scope.attempt_token,
                                row["generation"]) != scope:
            _fail("candidate_scope_mismatch")

    @_public_boundary("adapter_resolution_failed")
    def resolve_confirmed(self, key: str, stage: str) -> PrivateEffectReference:
        if not _hex(key) or type(stage) is not str or stage not in STAGES:
            _fail("candidate_scope_invalid")
        row = self.store.db.execute("""SELECT s.attempt_token,s.evidence_digest,
            s.effect_digest,a.generation FROM stages s JOIN attempts a
            ON a.token=s.attempt_token WHERE s.key=? AND s.stage=? AND s.status='confirmed'
            """, (key, stage)).fetchone()
        if row is None:
            _fail("confirmed_reference_missing")
        candidate = ReceiptCandidate(Scope(key, stage, row["attempt_token"], row["generation"]),
                                     row["evidence_digest"], row["effect_digest"])
        self._persisted_scope(candidate.scope)
        reference = self._resolve(candidate)
        self._verify_receipt(candidate, reference)
        return reference

    @_public_boundary("adapter_advance_failed")
    def advance(self, claim: Claim, perform: Callable[[Attempt], EffectResult]) -> Receipt:
        self._require_available()
        # Confirmed state cannot authorize a follow-on effect without its private
        # reference and trusted readback. Failure here precedes a new attempt.
        for stage, status in self.store.snapshot(claim.key)["stages"].items():
            if status == "confirmed":
                self.resolve_confirmed(claim.key, stage)

        def checked(attempt):
            scope = _attempt_scope(attempt)
            try:
                result = perform(attempt)
                if type(result) is not EffectResult:
                    _fail("effect_result_invalid")
                candidate = parse_receipt_candidate(result.candidate_json)
                if candidate.scope != scope:
                    _fail("candidate_scope_mismatch")
                reference = _reference(result.reference, scope, candidate.effect_digest)
                receipt = self._verify_receipt(candidate, reference)
                self.references.persist(reference)
                persisted = self._resolve(candidate)
                if persisted != reference:
                    _fail("private_reference_invalid")
                return receipt
            except Exception:
                _fail("effect_confirmation_uncertain")

        return self.store.advance(claim, checked)

    @_public_boundary("adapter_reconciliation_failed")
    def reconcile(self, raw: str) -> None:
        candidate = parse_reconciliation_candidate(raw)
        self._persisted_scope(candidate.scope)
        try:
            verified = self.authority.verify(candidate)
        except Exception:
            _fail("operator_authorization_required")
        expected = VerifiedAuthority(candidate.scope, candidate.decision,
                                     candidate.authority_digest, candidate.evidence_digest,
                                     candidate.drain_evidence_digest)
        if type(verified) is not VerifiedAuthority:
            _fail("operator_authorization_required")
        _validate_scope(verified.scope)
        if (type(verified.decision) is not str or not _hex(verified.authority_digest)
                or not _hex(verified.evidence_digest)
                or (verified.drain_evidence_digest is not None and not _hex(verified.drain_evidence_digest))
                or verified != expected):
            _fail("operator_authorization_required")
        receipt = None
        if candidate.receipt is not None:
            reference = self._resolve(candidate.receipt)
            receipt = self._verify_receipt(candidate.receipt, reference)
        request = Reconciliation(candidate.scope.key, candidate.scope.stage,
                                 candidate.scope.attempt_token, candidate.decision,
                                 candidate.authority_digest, candidate.evidence_digest, receipt)
        if self._authorized_request is not None:
            _fail("reconciliation_busy")
        self._authorized_request = request
        try:
            self.store.reconcile(request)
        finally:
            self._authorized_request = None
