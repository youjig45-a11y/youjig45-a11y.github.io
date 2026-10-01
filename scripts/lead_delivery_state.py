"""Isolated SQLite delivery state prototype. No HTTP or production integration.

Only synthetic fixtures are authorized here. Receipts and reconciliation authority
are caller-supplied references, not independently verified remote evidence.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterable
import uuid


STAGES = ("notion", "discord", "mark_read")


class StateError(Exception):
    """Fixed-category error; never includes raw identifiers or exception text."""


class Busy(StateError):
    pass


class StaleClaim(StateError):
    pass


class OutcomeUncertain(StateError):
    pass


class AlreadyComplete(StateError):
    pass


class ReconciliationDenied(StateError):
    pass


def digest(reference: str) -> str:
    if not isinstance(reference, str) or not reference.strip():
        raise StateError("invalid_reference")
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


def lead_key(form_id: str, submission_id: str) -> str:
    # Length-delimited JSON distinguishes ('ab', 'c') from ('a', 'bc').
    for value in (form_id, submission_id):
        if not isinstance(value, str) or not value.strip():
            raise StateError("invalid_identifier")
    return digest(json.dumps([form_id, submission_id], ensure_ascii=True, separators=(",", ":")))


def _check_digest(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise StateError("invalid_digest")


@dataclass(frozen=True)
class Claim:
    key: str
    token: str
    generation: int
    lease_until: float


@dataclass(frozen=True)
class Attempt:
    key: str
    stage: str
    token: str
    generation: int


@dataclass(frozen=True)
class Receipt:
    stage: str
    evidence_digest: str
    effect_digest: str


@dataclass(frozen=True)
class Reconciliation:
    key: str
    stage: str
    attempt_token: str
    decision: str  # confirmed, or independently established no_effect
    authority_digest: str
    evidence_digest: str
    receipt: Receipt | None = None


def preview_migration(records: Iterable[tuple[str, str]]) -> list[dict]:
    """Synthetic, read-only plan; never seed successes or touch a delivery store."""
    keys = sorted({lead_key(form, submission) for form, submission in records})
    return [{"key": key, "action": "review_existing_effects", "status": "unknown"} for key in keys]


class DeliveryStore:
    def __init__(
        self, path: Path, *, clock: Callable[[], float] = time.time,
        authorizer: Callable[[Reconciliation], bool] | None = None,
    ):
        path = Path(path)
        if not path.is_absolute():
            raise StateError("explicit_absolute_store_path_required")
        self.clock = clock
        self.authorizer = authorizer
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA synchronous = FULL")
        try:
            self._initialize()
        except Exception:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _now(self) -> float:
        instant = self.clock()
        if not isinstance(instant, (int, float)) or not math.isfinite(instant) or instant < 0:
            raise StateError("invalid_clock")
        return instant

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        else:
            try:
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def _initialize(self) -> None:
        # No migration of existing or unrelated stores is attempted.
        with self._transaction():
            tables = {r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            expected = {"leads", "stages", "attempts", "reconciliations"}
            if tables:
                if tables != expected or version != 1:
                    raise StateError("unsupported_store_schema")
                return
            if version != 0:
                raise StateError("unsupported_store_schema")
            self.db.execute("""CREATE TABLE leads (
                key TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
                owner TEXT, lease_until REAL, created_at REAL NOT NULL)""")
            self.db.execute("""CREATE TABLE stages (
                key TEXT NOT NULL REFERENCES leads(key), stage TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','inflight','uncertain','confirmed')),
                attempt_token TEXT, evidence_digest TEXT, effect_digest TEXT,
                PRIMARY KEY(key, stage))""")
            self.db.execute("""CREATE TABLE attempts (
                token TEXT PRIMARY KEY, key TEXT NOT NULL REFERENCES leads(key),
                stage TEXT NOT NULL, generation INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('prepared','uncertain','confirmed','reconciled_no_effect','reconciled_confirmed')),
                prepared_at REAL NOT NULL, finished_at REAL)""")
            self.db.execute("""CREATE TABLE reconciliations (
                attempt_token TEXT PRIMARY KEY REFERENCES attempts(token),
                decision TEXT NOT NULL, authority_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL, recorded_at REAL NOT NULL)""")
            self.db.execute("PRAGMA user_version = 1")

    def enroll(self, form_id: str, submission_id: str) -> str:
        key, instant = lead_key(form_id, submission_id), self._now()
        with self._transaction():
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO leads(key,created_at) VALUES(?,?)", (key, instant),
            ).rowcount
            if inserted:
                for stage in STAGES:
                    self.db.execute(
                        "INSERT INTO stages(key,stage,status) VALUES(?,?,'pending')", (key, stage),
                    )
            else:
                # Missing state is corruption, not permission to start delivery.
                self.snapshot(key)
        return key

    def snapshot(self, key: str) -> dict:
        _check_digest(key)
        rows = {row["stage"]: row for row in self.db.execute(
            "SELECT * FROM stages WHERE key=?", (key,),
        )}
        if set(rows) != set(STAGES):
            raise StateError("unknown_lead")
        return {
            "complete": all(rows[stage]["status"] == "confirmed" for stage in STAGES),
            "stages": {stage: rows[stage]["status"] for stage in STAGES},
            "receipts": {stage: {
                "evidence_digest": rows[stage]["evidence_digest"],
                "effect_digest": rows[stage]["effect_digest"],
            } for stage in STAGES},
        }

    def claim(self, key: str, *, lease_seconds: float = 60) -> Claim:
        _check_digest(key)
        if (not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds)
                or lease_seconds <= 0):
            raise StateError("invalid_lease")
        instant = self._now()
        uncertain = False
        result = None
        with self._transaction():
            lead = self.db.execute("SELECT * FROM leads WHERE key=?", (key,)).fetchone()
            if lead is None:
                raise StateError("unknown_lead")
            if lead["owner"] and lead["lease_until"] > instant:
                raise Busy("claim_busy")
            # An expired in-flight attempt is never inferred to have had no effect.
            self.db.execute("""UPDATE attempts SET status='uncertain',finished_at=?
                WHERE key=? AND status='prepared'""", (instant, key))
            self.db.execute("UPDATE stages SET status='uncertain' WHERE key=? AND status='inflight'", (key,))
            states = self.snapshot(key)["stages"]
            if "uncertain" in states.values():
                self.db.execute("UPDATE leads SET owner=NULL,lease_until=NULL WHERE key=?", (key,))
                uncertain = True
            elif all(state == "confirmed" for state in states.values()):
                raise AlreadyComplete("delivery_already_complete")
            else:
                token, generation = uuid.uuid4().hex, lead["generation"] + 1
                until = instant + lease_seconds
                if not math.isfinite(until):
                    raise StateError("invalid_lease")
                self.db.execute(
                    "UPDATE leads SET owner=?,generation=?,lease_until=? WHERE key=?",
                    (token, generation, until, key),
                )
                result = Claim(key, token, generation, until)
        if uncertain:
            raise OutcomeUncertain("reconciliation_required")
        return result

    def _require_claim(self, claim: Claim) -> None:
        lead = self.db.execute("SELECT * FROM leads WHERE key=?", (claim.key,)).fetchone()
        if (lead is None or lead["owner"] != claim.token or lead["generation"] != claim.generation
                or lead["lease_until"] <= self._now()):
            raise StaleClaim("claim_stale")

    def begin_attempt(self, claim: Claim) -> Attempt:
        with self._transaction():
            self._require_claim(claim)
            states = self.snapshot(claim.key)["stages"]
            stage = next((name for name in STAGES if states[name] != "confirmed"), None)
            if stage is None:
                raise AlreadyComplete("delivery_already_complete")
            if states[stage] != "pending":
                raise OutcomeUncertain("reconciliation_required")
            token = uuid.uuid4().hex
            self.db.execute("""INSERT INTO attempts
                (token,key,stage,generation,status,prepared_at) VALUES(?,?,?,?,'prepared',?)""",
                (token, claim.key, stage, claim.generation, self._now()))
            self.db.execute("""UPDATE stages SET status='inflight',attempt_token=?
                WHERE key=? AND stage=?""", (token, claim.key, stage))
        return Attempt(claim.key, stage, token, claim.generation)

    def _check_attempt(self, claim: Claim, attempt: Attempt) -> None:
        self._require_claim(claim)
        row = self.db.execute("SELECT * FROM attempts WHERE token=?", (attempt.token,)).fetchone()
        if (row is None or row["key"] != claim.key or attempt.key != claim.key
                or row["generation"] != claim.generation or attempt.generation != claim.generation
                or row["stage"] != attempt.stage or row["status"] != "prepared"):
            raise StaleClaim("attempt_stale")

    @staticmethod
    def _check_receipt(stage: str, receipt: Receipt) -> None:
        if not isinstance(receipt, Receipt) or receipt.stage != stage:
            raise StateError("invalid_receipt")
        _check_digest(receipt.evidence_digest)
        _check_digest(receipt.effect_digest)

    def confirm(self, claim: Claim, attempt: Attempt, receipt: Receipt) -> None:
        self._check_receipt(attempt.stage, receipt)
        with self._transaction():
            self._check_attempt(claim, attempt)
            self.db.execute("UPDATE attempts SET status='confirmed',finished_at=? WHERE token=?",
                            (self._now(), attempt.token))
            self.db.execute("""UPDATE stages SET status='confirmed',evidence_digest=?,effect_digest=?
                WHERE key=? AND stage=? AND attempt_token=?""",
                (receipt.evidence_digest, receipt.effect_digest, claim.key, attempt.stage, attempt.token))

    def mark_uncertain(self, claim: Claim, attempt: Attempt) -> None:
        with self._transaction():
            self._check_attempt(claim, attempt)
            self.db.execute("UPDATE attempts SET status='uncertain',finished_at=? WHERE token=?",
                            (self._now(), attempt.token))
            self.db.execute("UPDATE stages SET status='uncertain' WHERE key=? AND stage=?",
                            (claim.key, attempt.stage))

    def advance(self, claim: Claim, perform: Callable[[Attempt], Receipt]) -> Receipt:
        """Commit an intent, invoke an injected executor, then commit its receipt.

        No transport is provided. A failed/invalid/late reply is conservative
        uncertainty, including an HTTP rejection or timeout reported by a caller.
        """
        attempt = self.begin_attempt(claim)
        try:
            with self._transaction():
                self._check_attempt(claim, attempt)
            receipt = perform(attempt)
            self.confirm(claim, attempt, receipt)
            return receipt
        except Exception:
            try:
                self.mark_uncertain(claim, attempt)
            except StaleClaim:
                # Expired intent remains persisted; next claim converts it to
                # uncertainty and still cannot replay it.
                pass
            raise OutcomeUncertain("reconciliation_required") from None

    def release(self, claim: Claim) -> None:
        with self._transaction():
            self._require_claim(claim)
            self.db.execute("""UPDATE attempts SET status='uncertain',finished_at=?
                WHERE key=? AND status='prepared'""", (self._now(), claim.key))
            self.db.execute("UPDATE stages SET status='uncertain' WHERE key=? AND status='inflight'",
                            (claim.key,))
            self.db.execute("UPDATE leads SET owner=NULL,lease_until=NULL WHERE key=?", (claim.key,))

    def reconcile(self, request: Reconciliation) -> None:
        """Default-denied boundary; a future authorized operator adapter is needed.

        A digest is only a reference. The injected authorizer must verify exact
        operator authority and evidence outside this prototype; it is not supplied
        here. No env/CLI switch, remote readback or approval implementation exists.
        """
        _check_digest(request.key)
        _check_digest(request.authority_digest)
        _check_digest(request.evidence_digest)
        if request.stage not in STAGES or request.decision not in ("confirmed", "no_effect"):
            raise StateError("invalid_reconciliation")
        if request.decision == "confirmed":
            self._check_receipt(request.stage, request.receipt)
        elif request.receipt is not None:
            raise StateError("invalid_reconciliation")
        if self.authorizer is None or self.authorizer(request) is not True:
            raise ReconciliationDenied("operator_authorization_required")
        with self._transaction():
            lead = self.db.execute("SELECT * FROM leads WHERE key=?", (request.key,)).fetchone()
            if lead is None:
                raise StateError("unknown_lead")
            if lead["owner"] and lead["lease_until"] > self._now():
                raise Busy("claim_busy")
            row = self.db.execute("SELECT * FROM stages WHERE key=? AND stage=?",
                                  (request.key, request.stage)).fetchone()
            if row["status"] != "uncertain" or row["attempt_token"] != request.attempt_token:
                raise StateError("reconciliation_stale")
            confirmed = request.decision == "confirmed"
            self.db.execute("""INSERT INTO reconciliations
                (attempt_token,decision,authority_digest,evidence_digest,recorded_at) VALUES(?,?,?,?,?)""",
                (request.attempt_token, request.decision, request.authority_digest,
                 request.evidence_digest, self._now()))
            self.db.execute("UPDATE attempts SET status=?,finished_at=? WHERE token=?",
                            ("reconciled_confirmed" if confirmed else "reconciled_no_effect",
                             self._now(), request.attempt_token))
            self.db.execute("""UPDATE stages SET status=?,attempt_token=?,evidence_digest=?,effect_digest=?
                WHERE key=? AND stage=?""", (
                    "confirmed" if confirmed else "pending",
                    request.attempt_token if confirmed else None,
                    request.receipt.evidence_digest if confirmed else None,
                    request.receipt.effect_digest if confirmed else None,
                    request.key, request.stage,
                ))
            self.db.execute("UPDATE leads SET owner=NULL,lease_until=NULL WHERE key=?", (request.key,))
