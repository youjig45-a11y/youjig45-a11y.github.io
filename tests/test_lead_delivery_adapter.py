"""Offline fixtures only: no credentials, real references or network transport."""

from contextlib import closing
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lead_delivery_adapter as adapter
import lead_delivery_state as state

PRIVATE_SENTINEL = "SYNTHETIC_PRIVATE_SENTINEL_https://private.invalid/clock-db?key=synthetic-only"


class FailingDatabase:
    """Fault injection around the same temporary engine connection."""

    def __init__(self, database, fragment):
        self.database = database
        self.fragment = fragment
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.database, name)

    def execute(self, sql, *args):
        if self.fragment in sql and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError(PRIVATE_SENTINEL)
        return self.database.execute(sql, *args)


def scope_for(attempt):
    return adapter.Scope(attempt.key, attempt.stage, attempt.token, attempt.generation)


def proof(scope, value):
    return state.digest("synthetic-proof:" + json.dumps(asdict(scope), sort_keys=True) + value)


def receipt_object(scope, value):
    return {"version": 1, "scope": asdict(scope),
            "evidence_digest": proof(scope, value), "effect_digest": state.digest(value)}


def effect_result(attempt, value=None):
    value = value or "synthetic-private:" + attempt.stage
    scope = scope_for(attempt)
    return adapter.EffectResult(json.dumps(receipt_object(scope, value)),
                                adapter.PrivateEffectReference(scope, value))


class SyntheticReferences(adapter.PrivateReferenceStore):
    """Temporary SQLite fixture, never a selected production private backend."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS refs (
            key TEXT, stage TEXT, token TEXT, generation INTEGER, value TEXT,
            PRIMARY KEY(key,stage,token,generation))""")
        self.db.commit()

    def close(self):
        self.db.close()

    def require_available(self):
        return None

    def persist(self, reference):
        scope = reference.scope
        self.db.execute("INSERT OR IGNORE INTO refs VALUES(?,?,?,?,?)",
                        (scope.key, scope.stage, scope.attempt_token,
                         scope.generation, reference.value))
        self.db.commit()
        if self.resolve(scope, state.digest(reference.value)) != reference:
            raise RuntimeError("synthetic-private-reference-collision")

    def resolve(self, scope, effect_digest):
        row = self.db.execute("SELECT value FROM refs WHERE key=? AND stage=? AND token=? AND generation=?",
                              (scope.key, scope.stage, scope.attempt_token,
                               scope.generation)).fetchone()
        if row is None or state.digest(row[0]) != effect_digest:
            raise RuntimeError("synthetic-private-reference-unavailable")
        return adapter.PrivateEffectReference(scope, row[0])


class SyntheticReceipts(adapter.ReceiptVerifier):
    """Known deterministic fixture evidence, explicitly not real authenticity."""

    def require_available(self):
        return None

    def verify(self, candidate, reference):
        if (not reference.value.startswith("synthetic-private:")
                or candidate.evidence_digest != proof(candidate.scope, reference.value)
                or candidate.effect_digest != state.digest(reference.value)):
            raise RuntimeError("synthetic-receipt-denied")
        return adapter.VerifiedReceipt(candidate.scope, candidate.evidence_digest,
                                       candidate.effect_digest)


class SyntheticAuthority(adapter.AuthorityVerifier):
    def __init__(self, allowed=(), *, now=100, expires=200):
        self.allowed = tuple(allowed)
        self.now = now
        self.expires = expires
        self.calls = 0

    def verify(self, candidate):
        self.calls += 1
        if candidate not in self.allowed or self.now >= self.expires:
            raise RuntimeError("synthetic-authority-denied")
        return adapter.VerifiedAuthority(candidate.scope, candidate.decision,
                                         candidate.authority_digest, candidate.evidence_digest,
                                         candidate.drain_evidence_digest)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "engine.sqlite"
        self.ref_path = Path(self.temp.name) / "references.sqlite"
        self.refs = SyntheticReferences(self.ref_path)
        self.addCleanup(self.refs.close)
        self.instant = [100.0]
        self.worker = self.open_worker()
        self.addCleanup(lambda: self.worker.close() if self.worker is not None else None)
        self.key = self.worker.store.enroll("synthetic-form", "synthetic-submission")

    def open_worker(self, **kwargs):
        return adapter.DeliveryAdapter(self.path, references=self.refs,
                                       receipts=kwargs.pop("receipts", SyntheticReceipts()),
                                       clock=lambda: self.instant[0], **kwargs)

    def restart(self, **kwargs):
        self.worker.close()
        self.worker = self.open_worker(**kwargs)

    def uncertain_attempt(self):
        claim = self.worker.store.claim(self.key)
        attempt = self.worker.store.begin_attempt(claim)
        self.worker.store.mark_uncertain(claim, attempt)
        self.worker.store.release(claim)
        return attempt

    def reconciliation_object(self, attempt, decision="confirmed"):
        scope = scope_for(attempt)
        return {"version": 1, "scope": asdict(scope), "decision": decision,
                "authority_digest": state.digest("synthetic-operator"),
                "evidence_digest": state.digest("synthetic-readback"),
                "drain_evidence_digest": state.digest("synthetic-drain") if decision == "no_effect" else None,
                "receipt": receipt_object(scope, "synthetic-private:" + attempt.stage)
                if decision == "confirmed" else None}

    def permit(self, value, **kwargs):
        candidate = adapter.parse_reconciliation_candidate(json.dumps(value))
        authority = SyntheticAuthority([candidate], **kwargs)
        self.worker.authority = authority
        return authority

    def assert_uncertain(self):
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "uncertain")
        with self.assertRaises(state.OutcomeUncertain):
            self.worker.store.claim(self.key)

    def assert_sanitized(self, callback, error_type, category):
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output):
            with self.assertRaises(error_type) as caught:
                callback()
        error = caught.exception
        self.assertIs(type(error), error_type)
        self.assertEqual(str(error), category)
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        for text in (str(error), rendered, output.getvalue()):
            self.assertNotIn(PRIVATE_SENTINEL, text)
            self.assertNotIn("private.invalid/clock-db", text)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(error.__suppress_context__)
        self.assertIsNone(error.__cause__)

    def test_strict_receipt_parser_hides_private_values_in_repr(self):
        attempt = self.worker.store.begin_attempt(self.worker.store.claim(self.key))
        result = effect_result(attempt)
        candidate = adapter.parse_receipt_candidate(result.candidate_json)
        self.assertEqual(candidate.scope, scope_for(attempt))
        private = adapter.PrivateEffectReference(candidate.scope, "synthetic-private:secret")
        self.assertNotIn("secret", repr(private))
        self.assertNotIn(attempt.token, repr(candidate))

    def test_parser_rejects_self_authority_unknown_missing_and_duplicate_fields(self):
        attempt = self.worker.store.begin_attempt(self.worker.store.claim(self.key))
        value = json.loads(effect_result(attempt).candidate_json)
        for key in ("approved", "authorized", "verified", "fresh", "complete"):
            with self.subTest(key=key), self.assertRaises(adapter.AdapterError):
                adapter.parse_receipt_candidate(json.dumps({**value, key: True}))
        missing = dict(value)
        del missing["evidence_digest"]
        for raw in (json.dumps(missing), '{"version":1,"version":1}',
                    '{"scope":{"key":"a","key":"b"}}', json.dumps(value) + "{}",
                    "x" * 16385):
            with self.subTest(raw_kind=raw[:1]), self.assertRaises(adapter.AdapterError):
                adapter.parse_receipt_candidate(raw)

    def test_parser_rejects_coercion_bad_types_and_versions(self):
        attempt = self.worker.store.begin_attempt(self.worker.store.claim(self.key))
        value = json.loads(effect_result(attempt).candidate_json)
        for version in (True, "1", 1.0, 2):
            with self.subTest(version=version), self.assertRaises(adapter.AdapterError):
                adapter.parse_receipt_candidate(json.dumps({**value, "version": version}))
        for field, bad in (("key", "G" * 64), ("stage", "email"),
                           ("attempt_token", " "), ("generation", True),
                           ("generation", "1"), ("generation", 0)):
            with self.subTest(field=field, bad=bad), self.assertRaises(adapter.AdapterError):
                adapter.parse_receipt_candidate(json.dumps({**value, "scope": {**value["scope"], field: bad}}))
        for bad in (None, 123, "A" * 64, ""):
            with self.subTest(digest=bad), self.assertRaises(adapter.AdapterError):
                adapter.parse_receipt_candidate(json.dumps({**value, "effect_digest": bad}))

    def test_default_denial_precedes_intent_and_effect(self):
        with adapter.DeliveryAdapter(Path(self.temp.name) / "default.sqlite") as denied:
            key = denied.store.enroll("synthetic-default", "synthetic-submission")
            claim = denied.store.claim(key)
            effects = []
            with self.assertRaises(adapter.AdapterError):
                denied.advance(claim, lambda attempt: effects.append(attempt))
            self.assertEqual(effects, [])
            self.assertEqual(denied.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual(denied.store.snapshot(key)["stages"]["notion"], "pending")

    def test_explicit_denied_verifier_and_missing_reference_store_precede_effect(self):
        for receipts, refs in ((adapter.DeniedReceiptVerifier(), self.refs),
                               (SyntheticReceipts(), adapter.DeniedPrivateReferenceStore())):
            self.worker.receipts, self.worker.references = receipts, refs
            claim = self.worker.store.claim(self.key)
            with self.assertRaises(adapter.AdapterError):
                self.worker.advance(claim, lambda _: self.fail("effect must not run"))
            self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
            self.worker.store.release(claim)

    def test_other_lead_stage_attempt_and_generation_receipts_are_not_adopted(self):
        for index, (field, wrong) in enumerate((("key", "a" * 64), ("stage", "discord"),
                                                ("attempt_token", "b" * 32), ("generation", 7))):
            key = self.worker.store.enroll("synthetic-scope", "synthetic-" + str(index))
            claim = self.worker.store.claim(key)
            effects = []

            def wrong_scope(attempt):
                effects.append(attempt.stage)
                result = effect_result(attempt)
                value = json.loads(result.candidate_json)
                value["scope"][field] = wrong
                return replace(result, candidate_json=json.dumps(value))

            with self.subTest(field=field), self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, wrong_scope)
            self.assertEqual(effects, ["notion"])
            self.worker.store.release(claim)
            self.assertEqual(self.worker.store.snapshot(key)["stages"]["notion"], "uncertain")
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.store.claim(key)

    def test_truthy_and_wrong_verified_receipts_never_confirm(self):
        for index, result in enumerate((True, 1, None, object(), False)):
            key = self.worker.store.enroll("synthetic-verifier", "synthetic-" + str(index))
            claim = self.worker.store.claim(key)
            with patch.object(self.worker.receipts, "verify", return_value=result):
                with self.assertRaises(state.OutcomeUncertain):
                    self.worker.advance(claim, effect_result)
            self.worker.store.release(claim)
            self.assertEqual(self.worker.store.snapshot(key)["stages"]["notion"], "uncertain")
        key = self.worker.store.enroll("synthetic-verifier", "synthetic-wrong-binding")
        claim = self.worker.store.claim(key)
        wrong = adapter.VerifiedReceipt(adapter.Scope("a" * 64, "notion", "b" * 32, 1),
                                        "c" * 64, "d" * 64)
        with patch.object(self.worker.receipts, "verify", return_value=wrong):
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, effect_result)

    def test_invalid_reference_binding_and_hash_leave_uncertainty(self):
        for index, kind in enumerate(("scope", "value", "empty")):
            key = self.worker.store.enroll("synthetic-reference", "synthetic-" + str(index))
            claim = self.worker.store.claim(key)

            def wrong_reference(attempt):
                result = effect_result(attempt)
                reference = result.reference
                if kind == "scope":
                    reference = replace(reference, scope=replace(reference.scope, generation=9))
                else:
                    reference = replace(reference, value="" if kind == "empty" else "synthetic-private:wrong")
                return replace(result, reference=reference)

            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, wrong_reference)
            self.worker.store.release(claim)
            self.assertEqual(self.worker.store.snapshot(key)["stages"]["notion"], "uncertain")

    def test_verified_receipt_nested_generation_boolean_is_rejected(self):
        claim = self.worker.store.claim(self.key)

        def boolean_scope(candidate, reference):
            return adapter.VerifiedReceipt(replace(candidate.scope, generation=True),
                                           candidate.evidence_digest, candidate.effect_digest)

        with patch.object(self.worker.receipts, "verify", side_effect=boolean_scope):
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, effect_result)
        self.worker.store.release(claim)
        self.assert_uncertain()

    def test_private_reference_nested_generation_boolean_is_rejected(self):
        claim = self.worker.store.claim(self.key)

        def boolean_reference(attempt):
            result = effect_result(attempt)
            return replace(result, reference=replace(result.reference,
                           scope=replace(result.reference.scope, generation=True)))

        with self.assertRaises(state.OutcomeUncertain):
            self.worker.advance(claim, boolean_reference)
        self.worker.store.release(claim)
        self.assert_uncertain()

    def test_private_persistence_failure_after_effect_never_replays_on_restart(self):
        claim = self.worker.store.claim(self.key)
        effects = []

        def fake(attempt):
            effects.append(attempt.stage)
            return effect_result(attempt)

        with patch.object(self.refs, "persist", side_effect=RuntimeError("synthetic-private-secret")):
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, fake)
        self.worker.store.release(claim)
        self.restart()
        self.assert_uncertain()
        self.assertEqual(effects, ["notion"])

    def test_reference_readback_failure_after_persistence_is_uncertain(self):
        claim = self.worker.store.claim(self.key)
        original = self.refs.persist

        def persisted_but_unavailable(reference):
            original(reference)
            raise RuntimeError("synthetic-private-readback-failure")

        with patch.object(self.refs, "persist", side_effect=persisted_but_unavailable):
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, effect_result)
        self.worker.store.release(claim)
        self.assertEqual(self.refs.db.execute("SELECT COUNT(*) FROM refs").fetchone()[0], 1)
        self.restart()
        self.assert_uncertain()

    def test_wrong_resolver_return_after_persist_is_uncertain(self):
        claim = self.worker.store.claim(self.key)
        original = self.refs.persist

        def persist_then_poison(reference):
            original(reference)
            self.refs.resolve = lambda scope, digest: adapter.PrivateEffectReference(
                replace(scope, stage="discord"), reference.value)

        with patch.object(self.refs, "persist", side_effect=persist_then_poison):
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, effect_result)
        self.worker.store.release(claim)
        self.assert_uncertain()

    def test_verifier_exception_and_timeout_hide_private_exception_text(self):
        sentinel = "https://private.invalid/person@example.invalid?secret=synthetic-only"
        for index, mode in enumerate(("verify", "timeout")):
            key = self.worker.store.enroll("synthetic-errors", "synthetic-" + str(index))
            claim = self.worker.store.claim(key)
            output = io.StringIO()
            with patch("sys.stdout", output), patch("sys.stderr", output):
                if mode == "verify":
                    with patch.object(self.worker.receipts, "verify", side_effect=RuntimeError(sentinel)):
                        with self.assertRaises(state.OutcomeUncertain) as failure:
                            self.worker.advance(claim, effect_result)
                else:
                    def timeout(_):
                        raise TimeoutError(sentinel)
                    with self.assertRaises(state.OutcomeUncertain) as failure:
                        self.worker.advance(claim, timeout)
            self.assertEqual(str(failure.exception), "reconciliation_required")
            self.assertEqual(output.getvalue(), "")
            self.worker.store.release(claim)

    def test_receipt_confirmation_failure_retains_private_reference_and_blocks_replay(self):
        claim = self.worker.store.claim(self.key)
        captured = []

        def fake(attempt):
            captured.append(effect_result(attempt))
            return captured[-1]

        with patch.object(self.worker.store, "confirm", side_effect=sqlite3.OperationalError("synthetic-private-error")):
            with self.assertRaises(state.OutcomeUncertain):
                self.worker.advance(claim, fake)
        self.worker.store.release(claim)
        candidate = adapter.parse_receipt_candidate(captured[0].candidate_json)
        self.assertEqual(self.refs.resolve(candidate.scope, candidate.effect_digest), captured[0].reference)
        self.restart()
        self.assert_uncertain()

    def test_expiry_after_private_persistence_rejects_old_confirmation_and_replay(self):
        claim = self.worker.store.claim(self.key, lease_seconds=1)
        captured = []

        def slow(attempt):
            captured.append(effect_result(attempt))
            self.instant[0] += 2
            return captured[-1]

        with self.assertRaises(state.OutcomeUncertain):
            self.worker.advance(claim, slow)
        candidate = adapter.parse_receipt_candidate(captured[0].candidate_json)
        self.assertEqual(self.refs.resolve(candidate.scope, candidate.effect_digest), captured[0].reference)
        with self.assertRaises(state.OutcomeUncertain):
            self.worker.store.claim(self.key)
        self.assert_uncertain()
        with self.assertRaises(state.StaleClaim):
            self.worker.store.confirm(claim, state.Attempt(candidate.scope.key, candidate.scope.stage,
                                                          candidate.scope.attempt_token, candidate.scope.generation),
                                      state.Receipt(candidate.scope.stage, candidate.evidence_digest, candidate.effect_digest))

    def test_intent_is_committed_before_effect_and_private_data_stays_out_of_engine(self):
        claim = self.worker.store.claim(self.key)
        value = "synthetic-private:https://private.invalid/person@example.invalid"

        def fake(attempt):
            # sqlite's own context manager ends transactions, not connections.
            with closing(sqlite3.connect(str(self.path))) as reader:
                status = reader.execute("SELECT status FROM attempts WHERE token=?", (attempt.token,)).fetchone()[0]
                self.assertEqual(status, "prepared")
            return effect_result(attempt, value)

        self.worker.advance(claim, fake)
        self.worker.store.release(claim)
        self.assertNotIn(value.encode(), self.path.read_bytes())
        self.assertEqual(self.worker.resolve_confirmed(self.key, "notion").value, value)

    def test_restart_requires_reference_and_readback_before_next_effect(self):
        claim = self.worker.store.claim(self.key)
        self.worker.advance(claim, effect_result)
        self.worker.store.release(claim)
        self.restart()
        self.refs.db.execute("DELETE FROM refs")
        self.refs.db.commit()
        claim = self.worker.store.claim(self.key)
        with self.assertRaises(adapter.AdapterError):
            self.worker.advance(claim, lambda _: self.fail("follow-on effect must not run"))
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "confirmed")

    def test_default_authority_and_truthy_returns_cannot_reconcile(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt)
        self.refs.persist(effect_result(attempt).reference)
        with self.assertRaises(adapter.AdapterError):
            self.worker.reconcile(json.dumps(value))
        for result in (True, 1, None, object()):
            self.worker.authority = SyntheticAuthority()
            with patch.object(self.worker.authority, "verify", return_value=result):
                with self.assertRaises(adapter.AdapterError):
                    self.worker.reconcile(json.dumps(value))
        self.assert_uncertain()
        request = state.Reconciliation(self.key, attempt.stage, attempt.token, "no_effect",
                                       state.digest("synthetic-authority"), state.digest("synthetic-readback"))
        with self.assertRaises(state.ReconciliationDenied):
            self.worker.store.reconcile(request)

    def test_verified_authority_record_with_different_scope_is_denied(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        candidate = adapter.parse_reconciliation_candidate(json.dumps(value))
        self.worker.authority = SyntheticAuthority()
        wrong = adapter.VerifiedAuthority(replace(candidate.scope, generation=9), candidate.decision,
                                          candidate.authority_digest, candidate.evidence_digest,
                                          candidate.drain_evidence_digest)
        with patch.object(self.worker.authority, "verify", return_value=wrong):
            with self.assertRaises(adapter.AdapterError):
                self.worker.reconcile(json.dumps(value))
        self.assert_uncertain()

    def test_verified_authority_nested_generation_boolean_is_rejected(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        candidate = adapter.parse_reconciliation_candidate(json.dumps(value))
        self.worker.authority = SyntheticAuthority()
        wrong = adapter.VerifiedAuthority(replace(candidate.scope, generation=True), candidate.decision,
                                          candidate.authority_digest, candidate.evidence_digest,
                                          candidate.drain_evidence_digest)
        with patch.object(self.worker.authority, "verify", return_value=wrong):
            with self.assertRaises(adapter.AdapterError):
                self.worker.reconcile(json.dumps(value))
        self.assert_uncertain()

    def test_reconciliation_self_approval_and_scope_changes_do_not_reach_authority(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        authority = self.permit(value)
        for field in ("approved", "authorized", "verified"):
            with self.assertRaises(adapter.AdapterError):
                self.worker.reconcile(json.dumps({**value, field: True}))
        for field, wrong in (("key", "a" * 64), ("stage", "discord"),
                             ("attempt_token", "b" * 32), ("generation", 9)):
            wrong_value = {**value, "scope": {**value["scope"], field: wrong}}
            with self.subTest(field=field), self.assertRaises(adapter.AdapterError):
                self.worker.reconcile(json.dumps(wrong_value))
        self.assertEqual(authority.calls, 0)
        self.assert_uncertain()

    def test_confirmed_reconciliation_requires_receipt_and_private_reference(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt)
        self.permit(value)
        with self.assertRaises(adapter.AdapterError):
            self.worker.reconcile(json.dumps({**value, "receipt": None}))
        with self.assertRaises(adapter.AdapterError):
            self.worker.reconcile(json.dumps(value))
        self.assert_uncertain()

    def test_synthetic_authorized_adoption_runs_once_and_gate_resets(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt)
        self.refs.persist(effect_result(attempt).reference)
        self.permit(value)
        self.worker.reconcile(json.dumps(value))
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "confirmed")
        self.assertIsNone(self.worker._authorized_request)
        with self.assertRaises(state.StateError):
            self.worker.reconcile(json.dumps(value))
        self.assertIsNone(self.worker._authorized_request)
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0], 1)
        claim = self.worker.store.claim(self.key)
        effects = []

        def fake(attempt):
            effects.append(attempt.stage)
            return effect_result(attempt)

        self.worker.advance(claim, fake)
        self.assertEqual(effects, ["discord"])

    def test_live_claim_reconciliation_denies_and_clears_scoped_permit(self):
        claim = self.worker.store.claim(self.key)
        attempt = self.worker.store.begin_attempt(claim)
        self.worker.store.mark_uncertain(claim, attempt)
        value = self.reconciliation_object(attempt, "no_effect")
        self.permit(value)
        with self.assertRaises(state.Busy):
            self.worker.reconcile(json.dumps(value))
        self.assertIsNone(self.worker._authorized_request)
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0], 0)

    def test_no_effect_requires_trusted_drain_and_does_not_trust_hash_shape(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        authority = self.permit(value)
        for wrong in ({**value, "drain_evidence_digest": None},
                      {**value, "receipt": receipt_object(scope_for(attempt), "synthetic-private:notion")},
                      {**value, "drain_evidence_digest": state.digest("synthetic-untrusted-drain")},
                      {**value, "evidence_digest": state.digest("synthetic-untrusted-readback")}):
            with self.assertRaises(adapter.AdapterError):
                self.worker.reconcile(json.dumps(wrong))
        self.assert_uncertain()
        self.assertGreater(authority.calls, 0)
        self.worker.reconcile(json.dumps(value))
        new = self.worker.store.begin_attempt(self.worker.store.claim(self.key))
        self.assertEqual(new.stage, "notion")
        self.assertNotEqual(new.token, attempt.token)

    def test_expired_synthetic_authority_never_reconciles(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        self.permit(value, now=200, expires=200)
        with self.assertRaises(adapter.AdapterError):
            self.worker.reconcile(json.dumps(value))
        self.assert_uncertain()

    def test_migration_preview_cannot_supply_receipt_authority_or_dispatch(self):
        preview = state.preview_migration([("synthetic-history", "synthetic-unknown")])[0]
        self.assertEqual(preview["status"], "unknown")
        for parser in (adapter.parse_receipt_candidate, adapter.parse_reconciliation_candidate):
            with self.assertRaises(adapter.AdapterError):
                parser(json.dumps(preview))
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    def test_malformed_confirmed_digest_is_rejected_before_private_lookup(self):
        claim = self.worker.store.claim(self.key)
        self.worker.advance(claim, effect_result)
        self.worker.store.release(claim)
        self.worker.store.db.execute("UPDATE stages SET evidence_digest='malformed' WHERE key=? AND stage='notion'",
                                     (self.key,))
        with patch.object(self.refs, "resolve", side_effect=AssertionError("lookup must not run")) as resolver:
            with self.assertRaises(adapter.AdapterError):
                self.worker.resolve_confirmed(self.key, "notion")
        resolver.assert_not_called()

    def test_clock_failure_before_begin_is_sanitized_without_intent_or_effect(self):
        claim = self.worker.store.claim(self.key)
        effects = []

        def broken_clock():
            raise RuntimeError(PRIVATE_SENTINEL)

        self.worker.store.clock = broken_clock
        self.assert_sanitized(lambda: self.worker.advance(claim, lambda attempt: effects.append(attempt)),
                              adapter.AdapterError, "adapter_advance_failed")
        self.assertEqual(effects, [])
        self.assertFalse(self.worker.store.db.in_transaction)
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "pending")

    def test_db_failure_recording_uncertainty_preserves_intent_and_never_replays(self):
        claim = self.worker.store.claim(self.key, lease_seconds=1)
        effects = []
        self.worker.store.db = FailingDatabase(self.worker.store.db, "UPDATE attempts SET status='uncertain'")

        def possible_effect(attempt):
            effects.append(attempt.stage)
            raise RuntimeError(PRIVATE_SENTINEL)

        self.assert_sanitized(lambda: self.worker.advance(claim, possible_effect),
                              adapter.AdapterError, "adapter_advance_failed")
        self.assertEqual(effects, ["notion"])
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "inflight")
        self.assertEqual(self.worker.store.db.execute("SELECT status FROM attempts").fetchone()[0], "prepared")
        self.assertFalse(self.worker.store.db.in_transaction)
        self.restart()
        self.instant[0] += 2
        with self.assertRaises(state.OutcomeUncertain):
            self.worker.store.claim(self.key)
        self.assert_uncertain()
        self.assertEqual(effects, ["notion"])

    def test_verified_reconcile_clock_failure_is_sanitized_and_clears_permit(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        self.permit(value)

        def broken_clock():
            raise RuntimeError(PRIVATE_SENTINEL)

        self.worker.store.clock = broken_clock
        self.assert_sanitized(lambda: self.worker.reconcile(json.dumps(value)),
                              adapter.AdapterError, "adapter_reconciliation_failed")
        self.assertIsNone(self.worker._authorized_request)
        self.assertFalse(self.worker.store.db.in_transaction)
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0], 0)
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "uncertain")
        self.worker.store.clock = lambda: self.instant[0]
        self.assert_uncertain()

    def test_verified_reconcile_commit_failure_rolls_back_and_hides_db_exception(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        self.permit(value)
        self.worker.store.db = FailingDatabase(self.worker.store.db, "COMMIT")
        self.assert_sanitized(lambda: self.worker.reconcile(json.dumps(value)),
                              adapter.AdapterError, "adapter_reconciliation_failed")
        self.assertTrue(self.worker.store.db.failed)
        self.assertIsNone(self.worker._authorized_request)
        self.assertFalse(self.worker.store.db.in_transaction)
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0], 0)
        self.assertEqual(self.worker.store.db.execute("SELECT status FROM attempts").fetchone()[0], "uncertain")
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "uncertain")
        self.restart()
        self.assert_uncertain()

    def test_reconcile_scope_db_failure_precedes_authority_and_is_sanitized(self):
        attempt = self.uncertain_attempt()
        value = self.reconciliation_object(attempt, "no_effect")
        authority = self.permit(value)
        self.worker.store.db = FailingDatabase(self.worker.store.db, "SELECT key,stage,generation")
        self.assert_sanitized(lambda: self.worker.reconcile(json.dumps(value)),
                              adapter.AdapterError, "adapter_reconciliation_failed")
        self.assertEqual(authority.calls, 0)
        self.assertIsNone(self.worker._authorized_request)
        self.assert_uncertain()

    def test_constructor_database_failure_is_sanitized(self):
        path = Path(self.temp.name) / "synthetic-failed-open.sqlite"
        with patch("lead_delivery_state.sqlite3.connect", side_effect=sqlite3.OperationalError(PRIVATE_SENTINEL)):
            self.assert_sanitized(lambda: adapter.DeliveryAdapter(path),
                                  adapter.AdapterError, "adapter_initialization_failed")
        self.assertFalse(path.exists())

    def test_confirmed_resolver_database_failure_is_sanitized_without_new_attempt(self):
        claim = self.worker.store.claim(self.key)
        self.worker.advance(claim, effect_result)
        self.worker.store.release(claim)
        self.worker.store.db = FailingDatabase(self.worker.store.db, "SELECT s.attempt_token")
        self.assert_sanitized(lambda: self.worker.resolve_confirmed(self.key, "notion"),
                              adapter.AdapterError, "adapter_resolution_failed")
        self.assertEqual(self.worker.store.snapshot(self.key)["stages"]["notion"], "confirmed")
        self.assertEqual(self.worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_close_database_failure_is_sanitized_and_connection_is_later_closed(self):
        with patch.object(self.worker.store, "close", side_effect=sqlite3.OperationalError(PRIVATE_SENTINEL)):
            self.assert_sanitized(self.worker.close, adapter.AdapterError, "adapter_close_failed")
        self.worker.close()
        self.worker = None

    def test_known_engine_semantic_errors_keep_exact_type_and_fixed_category(self):
        claim = self.worker.store.claim(self.key)
        errors = [(state.Busy, "claim_busy"), (state.StaleClaim, "attempt_stale"),
                  (state.OutcomeUncertain, "reconciliation_required"),
                  (state.AlreadyComplete, "delivery_already_complete"),
                  (state.ReconciliationDenied, "operator_authorization_required"),
                  (state.StateError, "invalid_clock")]
        for error_type, category in errors:
            with self.subTest(category=category):
                with patch.object(self.worker.store, "advance", side_effect=error_type(category)):
                    self.assert_sanitized(lambda: self.worker.advance(claim, effect_result), error_type, category)

    def test_private_messages_in_semantic_error_classes_are_not_allowlisted(self):
        claim = self.worker.store.claim(self.key)
        for error in (state.StateError(PRIVATE_SENTINEL), state.Busy(PRIVATE_SENTINEL),
                      state.Busy("claim_busy", PRIVATE_SENTINEL), adapter.AdapterError(PRIVATE_SENTINEL)):
            with self.subTest(error_type=type(error).__name__):
                with patch.object(self.worker.store, "advance", side_effect=error):
                    self.assert_sanitized(lambda: self.worker.advance(claim, effect_result),
                                          adapter.AdapterError, "adapter_advance_failed")

    def assert_abrupt_exit_recovery(self, cutpoint, expected_references, *, confirmed=False):
        runner = r'''
import os, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from test_lead_delivery_adapter import SyntheticReferences, SyntheticReceipts, effect_result
from lead_delivery_adapter import DeliveryAdapter
cutpoint = sys.argv[3]
engine, private, effects = map(Path, sys.argv[4:7])
class AbruptReferences(SyntheticReferences):
    def persist(self, reference):
        if cutpoint == "before_private_commit":
            os._exit(86)
        super().persist(reference)
        if cutpoint == "after_private_commit":
            os._exit(86)
refs = AbruptReferences(private)
worker = DeliveryAdapter(engine, references=refs, receipts=SyntheticReceipts(), clock=lambda: 100)
key = worker.store.enroll("synthetic-crash-form", "synthetic-crash-submission")
claim = worker.store.claim(key, lease_seconds=60)
if cutpoint == "after_engine_confirm":
    original = worker.store.confirm
    def crash_after_confirm(*args):
        original(*args)
        os._exit(86)
    worker.store.confirm = crash_after_confirm
def fake(attempt):
    assert attempt.stage == "notion"
    with effects.open("a", encoding="utf-8") as effect_log:
        effect_log.write("notion\n")
        effect_log.flush()
        os.fsync(effect_log.fileno())
    return effect_result(attempt)
worker.advance(claim, fake)
raise AssertionError("abrupt_exit_not_reached")
'''
        engine = Path(self.temp.name) / "abrupt-engine.sqlite"
        private = Path(self.temp.name) / "abrupt-references.sqlite"
        effects = Path(self.temp.name) / "abrupt-fake-effects.txt"
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", runner, str(SCRIPTS),
             str(Path(__file__).resolve().parent), cutpoint, str(engine), str(private), str(effects)],
            env={"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")},
            text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(effects.read_text(encoding="utf-8").splitlines(), ["notion"])
        key = state.lead_key("synthetic-crash-form", "synthetic-crash-submission")
        refs = SyntheticReferences(private)
        self.addCleanup(refs.close)
        with adapter.DeliveryAdapter(engine, references=refs, receipts=SyntheticReceipts(),
                                     clock=lambda: 120) as worker:
            with self.assertRaisesRegex(state.Busy, "^claim_busy$"):
                worker.store.claim(key)
            self.assertEqual(worker.store.snapshot(key)["stages"], {
                "notion": "confirmed" if confirmed else "inflight",
                "discord": "pending", "mark_read": "pending",
            })
        with adapter.DeliveryAdapter(engine, references=refs, receipts=SyntheticReceipts(),
                                     clock=lambda: 161) as worker:
            if confirmed:
                claim = worker.store.claim(key)
                followon = []
                def fake_next(attempt):
                    self.assertEqual(worker.resolve_confirmed(key, "notion").value,
                                     "synthetic-private:notion")
                    self.assertIn(attempt.stage, ("discord", "mark_read"))
                    followon.append(attempt.stage)
                    return effect_result(attempt)
                worker.advance(claim, fake_next)
                worker.advance(claim, fake_next)
                self.assertEqual(followon, ["discord", "mark_read"])
                self.assertTrue(worker.store.snapshot(key)["complete"])
                worker.store.release(claim)
            else:
                with self.assertRaisesRegex(state.OutcomeUncertain, "^reconciliation_required$"):
                    worker.store.claim(key)
                snapshot = worker.store.snapshot(key)
                self.assertEqual(snapshot["stages"], {
                    "notion": "uncertain", "discord": "pending", "mark_read": "pending",
                })
                self.assertFalse(snapshot["complete"])
                row = worker.store.db.execute("SELECT token,generation FROM attempts WHERE key=?",
                                              (key,)).fetchone()
                attempt = state.Attempt(key, "notion", row["token"], row["generation"])
                with self.assertRaisesRegex(adapter.AdapterError, "^operator_authorization_required$"):
                    worker.reconcile(json.dumps(self.reconciliation_object(attempt)))
                self.assertEqual(worker.store.snapshot(key), snapshot)
            self.assertEqual(worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
                             3 if confirmed else 1)
            self.assertEqual(refs.db.execute("SELECT COUNT(*) FROM refs").fetchone()[0],
                             expected_references)
            self.assertEqual(effects.read_text(encoding="utf-8").splitlines(), ["notion"])

    def test_abrupt_exit_before_private_commit_stays_uncertain_without_replay(self):
        self.assert_abrupt_exit_recovery("before_private_commit", 0)

    def test_abrupt_exit_after_private_commit_retains_reference_without_confirming(self):
        self.assert_abrupt_exit_recovery("after_private_commit", 1)

    def test_abrupt_exit_after_engine_confirm_resumes_without_notion_replay(self):
        self.assert_abrupt_exit_recovery("after_engine_confirm", 3, confirmed=True)

    def test_actual_two_process_restart_resolves_private_identity_without_notion_replay(self):
        runner = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from test_lead_delivery_adapter import SyntheticReferences, SyntheticReceipts, effect_result
from lead_delivery_adapter import DeliveryAdapter
refs = SyntheticReferences(Path(sys.argv[4]))
try:
    with DeliveryAdapter(Path(sys.argv[3]), references=refs, receipts=SyntheticReceipts()) as worker:
        key = worker.store.enroll("synthetic-process-form", "synthetic-process-submission")
        claim = worker.store.claim(key)
        effects = []
        def fake(attempt):
            if attempt.stage != "notion":
                assert worker.resolve_confirmed(key, "notion").value == "synthetic-private:notion"
            effects.append(attempt.stage)
            return effect_result(attempt)
        for _ in range(int(sys.argv[5])):
            worker.advance(claim, fake)
        complete = worker.store.snapshot(key)["complete"]
        worker.store.release(claim)
        print(json.dumps({"effects": effects, "complete": complete}))
finally:
    refs.close()
'''
        engine = Path(self.temp.name) / "process-engine.sqlite"
        private = Path(self.temp.name) / "process-references.sqlite"
        outcomes = []
        for count in (1, 2):
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", runner, str(SCRIPTS),
                 str(Path(__file__).resolve().parent), str(engine), str(private), str(count)],
                env={"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")},
                text=True, capture_output=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            outcomes.append(json.loads(result.stdout))
        self.assertEqual(outcomes, [{"effects": ["notion"], "complete": False},
                                    {"effects": ["discord", "mark_read"], "complete": True}])


if __name__ == "__main__":
    unittest.main()
