"""Synthetic SQLite/fake executor proof; never contacts a delivery service."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lead_delivery_state.py"
SPEC = importlib.util.spec_from_file_location("lead_delivery_state", SCRIPT)
state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = state
SPEC.loader.exec_module(state)


def receipt(stage):
    return state.Receipt(stage, state.digest("synthetic-evidence-" + stage),
                         state.digest("synthetic-effect-" + stage))


class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-lead-state-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.sqlite"
        self.instant = [100.0]
        self.store = state.DeliveryStore(self.path, clock=lambda: self.instant[0])
        self.addCleanup(self.store.close)
        self.key = self.store.enroll("synthetic-form", "synthetic-submission")

    def reconcile_request(self, attempt, decision="confirmed"):
        return state.Reconciliation(
            self.key, attempt.stage, attempt.token, decision,
            state.digest("synthetic-operator-authorization"), state.digest("synthetic-readback"),
            receipt(attempt.stage) if decision == "confirmed" else None,
        )

    def test_composite_key_is_unambiguous_and_raw_identifiers_are_not_stored(self):
        self.assertNotEqual(state.lead_key("ab", "c"), state.lead_key("a", "bc"))
        self.assertNotEqual(state.lead_key("other-form", "synthetic-submission"), self.key)
        self.assertEqual(self.store.enroll("synthetic-form", "synthetic-submission"), self.key)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM leads").fetchone()[0], 1)
        stored = self.path.read_bytes()
        self.assertNotIn(b"synthetic-form", stored)
        self.assertNotIn(b"synthetic-submission", stored)

    def test_simultaneous_workers_only_one_can_claim(self):
        barrier = threading.Barrier(6)

        def contender(_):
            with state.DeliveryStore(self.path, clock=lambda: 100.0) as store:
                barrier.wait(timeout=5)
                try:
                    return store.claim(self.key)
                except state.Busy:
                    return None

        with ThreadPoolExecutor(max_workers=6) as workers:
            claims = list(workers.map(contender, range(6)))
        winners = [claim for claim in claims if claim]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].generation, 1)

    def test_attempt_is_committed_before_fake_effect(self):
        claim = self.store.claim(self.key)

        def fake_effect(attempt):
            with state.DeliveryStore(self.path, clock=lambda: 100.0) as observer:
                self.assertEqual(observer.snapshot(self.key)["stages"]["notion"], "inflight")
                row = observer.db.execute("SELECT status FROM attempts WHERE token=?",
                                          (attempt.token,)).fetchone()
                self.assertEqual(row[0], "prepared")
                with self.assertRaises(state.Busy):
                    observer.claim(self.key)
            return receipt(attempt.stage)

        self.store.advance(claim, fake_effect)
        self.assertEqual(self.store.snapshot(self.key)["stages"]["notion"], "confirmed")

    def test_live_claim_fencing_rejects_forged_generation_and_token(self):
        claim = self.store.claim(self.key)
        for forged in (replace(claim, generation=0), replace(claim, token="synthetic-other-worker")):
            with self.assertRaises(state.StaleClaim):
                self.store.begin_attempt(forged)
        self.assertEqual(self.store.snapshot(self.key)["stages"]["notion"], "pending")

    def test_expired_idle_claim_can_resume_but_old_owner_cannot_start_effect(self):
        old = self.store.claim(self.key, lease_seconds=5)
        self.instant[0] = 105.0
        new = self.store.claim(self.key)
        self.assertEqual(new.generation, old.generation + 1)
        effects = []
        with self.assertRaises(state.StaleClaim):
            self.store.advance(old, lambda attempt: effects.append(attempt.stage))
        self.assertEqual(effects, [])
        self.store.advance(new, lambda attempt: receipt(attempt.stage))

    def test_expired_prepared_attempt_blocks_new_owner_and_stale_confirmation(self):
        old = self.store.claim(self.key, lease_seconds=5)
        attempt = self.store.begin_attempt(old)
        self.instant[0] = 105.0
        for _ in range(2):
            with self.assertRaises(state.OutcomeUncertain):
                self.store.claim(self.key)
        with self.assertRaises(state.StaleClaim):
            self.store.confirm(old, attempt, receipt("notion"))
        self.assertEqual(self.store.snapshot(self.key)["stages"]["notion"], "uncertain")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_lease_expiring_between_intent_and_executor_skips_executor(self):
        claim = self.store.claim(self.key, lease_seconds=5)
        begin = self.store.begin_attempt

        def expire_after_intent(current):
            attempt = begin(current)
            self.instant[0] = 105.0
            return attempt

        effects = []
        with patch.object(self.store, "begin_attempt", side_effect=expire_after_intent):
            with self.assertRaises(state.OutcomeUncertain):
                self.store.advance(claim, lambda attempt: effects.append(attempt.stage))
        self.assertEqual(effects, [])
        with self.assertRaises(state.OutcomeUncertain):
            self.store.claim(self.key)

    def test_external_inflight_effect_cannot_be_fenced_so_new_owner_never_replays(self):
        claim = self.store.claim(self.key, lease_seconds=5)
        effects = []

        def delayed_external_effect(attempt):
            self.instant[0] = 105.0
            with state.DeliveryStore(self.path, clock=lambda: 105.0) as contender:
                with self.assertRaises(state.OutcomeUncertain):
                    contender.claim(self.key)
            # An external effect already in progress can still arrive late. Local
            # fencing cannot cancel it; the successor must continue to block.
            effects.append(attempt.stage)
            return receipt(attempt.stage)

        with self.assertRaises(state.OutcomeUncertain):
            self.store.advance(claim, delayed_external_effect)
        self.assertEqual(effects, ["notion"])
        with self.assertRaises(state.OutcomeUncertain):
            self.store.claim(self.key)
        self.assertFalse(self.store.snapshot(self.key)["complete"])

    def test_crash_after_intent_persists_until_expiry_without_replay(self):
        claim = self.store.claim(self.key, lease_seconds=5)
        self.store.begin_attempt(claim)
        with state.DeliveryStore(self.path, clock=lambda: 104.0) as restarted:
            with self.assertRaises(state.Busy):
                restarted.claim(self.key)
        with state.DeliveryStore(self.path, clock=lambda: 105.0) as restarted:
            with self.assertRaises(state.OutcomeUncertain):
                restarted.claim(self.key)
            self.assertEqual(restarted.snapshot(self.key)["stages"]["notion"], "uncertain")

    def test_partial_confirmed_notion_resumes_at_discord_after_restart(self):
        claim = self.store.claim(self.key)
        self.store.advance(claim, lambda attempt: receipt(attempt.stage))
        self.store.release(claim)
        with state.DeliveryStore(self.path, clock=lambda: 100.0) as restarted:
            resumed = restarted.claim(self.key)
            effects = []

            def fake_effect(attempt):
                effects.append(attempt.stage)
                return receipt(attempt.stage)

            restarted.advance(resumed, fake_effect)
            restarted.advance(resumed, fake_effect)
            self.assertEqual(effects, ["discord", "mark_read"])
            self.assertTrue(restarted.snapshot(self.key)["complete"])
            restarted.release(resumed)
            with self.assertRaises(state.AlreadyComplete):
                restarted.claim(self.key)

    def test_notion_confirmation_requires_scoped_evidence_and_effect_reference(self):
        claim = self.store.claim(self.key)
        attempt = self.store.begin_attempt(claim)
        bad = [receipt("discord"), state.Receipt("notion", "", state.digest("synthetic-page")),
               state.Receipt("notion", state.digest("synthetic-receipt"), "")]
        for invalid in bad:
            with self.assertRaises(state.StateError):
                self.store.confirm(claim, attempt, invalid)
        self.assertFalse(self.store.snapshot(self.key)["complete"])
        self.store.confirm(claim, attempt, receipt("notion"))
        with self.assertRaises(state.StaleClaim):
            self.store.confirm(claim, attempt, receipt("notion"))

    def test_discord_failure_does_not_reset_confirmed_notion(self):
        claim = self.store.claim(self.key)
        self.store.advance(claim, lambda attempt: receipt(attempt.stage))
        effects = []

        def failed_discord(attempt):
            effects.append(attempt.stage)
            raise RuntimeError("synthetic HTTP failure; delivery outcome unknown")

        with self.assertRaises(state.OutcomeUncertain):
            self.store.advance(claim, failed_discord)
        self.store.release(claim)
        with state.DeliveryStore(self.path, clock=lambda: 100.0) as restarted:
            with self.assertRaises(state.OutcomeUncertain):
                restarted.claim(self.key)
            self.assertEqual(restarted.snapshot(self.key)["stages"], {
                "notion": "confirmed", "discord": "uncertain", "mark_read": "pending",
            })
        self.assertEqual(effects, ["discord"])

    def test_timeout_after_possible_effect_requires_default_denied_reconciliation(self):
        claim = self.store.claim(self.key)
        attempted = []

        def timeout(attempt):
            attempted.append(attempt)
            raise TimeoutError("synthetic private exception must not be persisted")

        with self.assertRaises(state.OutcomeUncertain) as failure:
            self.store.advance(claim, timeout)
        self.assertEqual(str(failure.exception), "reconciliation_required")
        self.store.release(claim)
        request = self.reconcile_request(attempted[0])
        with self.assertRaises(state.ReconciliationDenied):
            self.store.reconcile(request)
        with self.assertRaises(state.OutcomeUncertain):
            self.store.claim(self.key)
        self.assertNotIn(b"private exception", self.path.read_bytes())

    def test_authorized_fake_reconciliation_can_adopt_confirmed_effect_without_replay(self):
        claim = self.store.claim(self.key)
        attempt = self.store.begin_attempt(claim)
        self.store.mark_uncertain(claim, attempt)
        self.store.release(claim)
        request = self.reconcile_request(attempt)
        with state.DeliveryStore(self.path, clock=lambda: 100.0,
                                 authorizer=lambda presented: presented == request) as operator:
            operator.reconcile(request)
            resumed = operator.claim(self.key)
            next_attempt = operator.begin_attempt(resumed)
            self.assertEqual(next_attempt.stage, "discord")
            with self.assertRaises(state.StateError):
                operator.reconcile(request)

    def test_only_authorized_no_effect_decision_allows_a_new_attempt(self):
        claim = self.store.claim(self.key)
        attempt = self.store.begin_attempt(claim)
        self.store.mark_uncertain(claim, attempt)
        self.store.release(claim)
        request = self.reconcile_request(attempt, "no_effect")
        with state.DeliveryStore(self.path, clock=lambda: 100.0,
                                 authorizer=lambda presented: presented == request) as operator:
            operator.reconcile(request)
            resumed = operator.claim(self.key)
            retry = operator.begin_attempt(resumed)
            self.assertEqual(retry.stage, "notion")
            self.assertNotEqual(retry.token, attempt.token)
            rows = operator.db.execute("SELECT status FROM attempts ORDER BY rowid").fetchall()
            self.assertEqual([row[0] for row in rows], ["reconciled_no_effect", "prepared"])

    def test_reconciliation_cannot_steal_live_claim_or_accept_wrong_attempt(self):
        claim = self.store.claim(self.key)
        attempt = self.store.begin_attempt(claim)
        self.store.mark_uncertain(claim, attempt)
        request = self.reconcile_request(attempt)
        with state.DeliveryStore(self.path, clock=lambda: 100.0,
                                 authorizer=lambda _: True) as operator:
            with self.assertRaises(state.Busy):
                operator.reconcile(request)
            self.store.release(claim)
            with self.assertRaises(state.StateError):
                operator.reconcile(replace(request, attempt_token="synthetic-wrong-attempt"))
            self.assertEqual(operator.db.execute("SELECT count(*) FROM reconciliations").fetchone()[0], 0)

    def test_receipt_commit_loss_is_uncertain_and_not_replayed(self):
        claim = self.store.claim(self.key)
        effects = []

        def fake_effect(attempt):
            effects.append(attempt.stage)
            return receipt(attempt.stage)

        with patch.object(self.store, "confirm", side_effect=sqlite3.OperationalError("synthetic commit loss")):
            with self.assertRaises(state.OutcomeUncertain):
                self.store.advance(claim, fake_effect)
        self.store.release(claim)
        with self.assertRaises(state.OutcomeUncertain):
            self.store.claim(self.key)
        self.assertEqual(effects, ["notion"])

    def test_failed_receipt_commit_rolls_back_and_retains_durable_intent(self):
        real = self.store.db

        class FailOneCommit:
            fail = False

            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, parameters=()):
                if sql == "COMMIT" and self.fail:
                    self.fail = False
                    raise sqlite3.OperationalError("synthetic receipt commit rejected")
                return real.execute(sql, parameters)

        proxy = FailOneCommit()
        self.store.db = proxy
        claim = self.store.claim(self.key)
        effects = []

        def fake_effect(attempt):
            effects.append(attempt.stage)
            proxy.fail = True
            return receipt(attempt.stage)

        with self.assertRaises(state.OutcomeUncertain):
            self.store.advance(claim, fake_effect)
        self.assertFalse(real.in_transaction)
        self.store.release(claim)
        with state.DeliveryStore(self.path, clock=lambda: 100.0) as restarted:
            self.assertEqual(restarted.snapshot(self.key)["stages"]["notion"], "uncertain")
            with self.assertRaises(state.OutcomeUncertain):
                restarted.claim(self.key)
        self.assertEqual(effects, ["notion"])

    def test_all_stage_receipts_are_required_for_complete(self):
        claim = self.store.claim(self.key)
        for index, stage in enumerate(state.STAGES):
            self.assertFalse(self.store.snapshot(self.key)["complete"])
            self.store.advance(claim, lambda attempt: receipt(attempt.stage))
            snap = self.store.snapshot(self.key)
            self.assertEqual(snap["receipts"][stage]["effect_digest"], receipt(stage).effect_digest)
            self.assertEqual(snap["complete"], index == 2)
        with self.assertRaises(state.AlreadyComplete):
            self.store.begin_attempt(claim)

    def test_release_with_unfinished_attempt_preserves_uncertainty(self):
        claim = self.store.claim(self.key)
        self.store.begin_attempt(claim)
        self.store.release(claim)
        with self.assertRaises(state.OutcomeUncertain):
            self.store.claim(self.key)

    def test_migration_preview_is_unknown_only_and_leaves_store_unchanged(self):
        before = self.store.db.total_changes
        preview = state.preview_migration([
            ("synthetic-legacy-form", "synthetic-legacy-submission"),
            ("synthetic-legacy-form", "synthetic-legacy-submission"),
        ])
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["status"], "unknown")
        self.assertEqual(preview[0]["action"], "review_existing_effects")
        self.assertNotIn("synthetic-legacy", json.dumps(preview))
        self.assertEqual(self.store.db.total_changes, before)

    def test_unknown_store_schema_is_not_migrated(self):
        unrelated = Path(self.temp.name) / "unrelated.sqlite"
        with closing(sqlite3.connect(unrelated)) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        with self.assertRaises(state.StateError):
            state.DeliveryStore(unrelated)
        with closing(sqlite3.connect(unrelated)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [("unrelated",)])

    def test_missing_existing_stage_is_not_silently_reseeded(self):
        self.store.db.execute("DELETE FROM stages WHERE key=? AND stage='notion'", (self.key,))
        with self.assertRaises(state.StateError):
            self.store.enroll("synthetic-form", "synthetic-submission")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM stages").fetchone()[0], 2)

    def test_absolute_path_identifiers_and_lease_are_validated(self):
        with self.assertRaises(state.StateError):
            state.DeliveryStore(Path("relative.sqlite"))
        for invalid in (None, "", " ", 1):
            with self.assertRaises(state.StateError):
                state.lead_key("synthetic", invalid)
        for seconds in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(state.StateError):
                self.store.claim(self.key, lease_seconds=seconds)

    def test_actual_separate_process_fake_runner_resumes_without_notion_replay(self):
        runner = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from lead_delivery_state import DeliveryStore, Receipt, digest
with DeliveryStore(Path(sys.argv[2])) as store:
    key = store.enroll("synthetic-process-form", "synthetic-process-submission")
    claim = store.claim(key)
    effects = []
    def fake(attempt):
        effects.append(attempt.stage)
        return Receipt(attempt.stage, digest("synthetic-evidence-" + attempt.stage),
                       digest("synthetic-effect-" + attempt.stage))
    for _ in range(int(sys.argv[3])):
        store.advance(claim, fake)
    complete = store.snapshot(key)["complete"]
    store.release(claim)
    print(json.dumps({"effects": effects, "complete": complete}))
'''
        db_path = Path(self.temp.name) / "separate-process.sqlite"
        env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")}
        outcomes = []
        for count in (1, 2):
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", runner, str(SCRIPT.parent), str(db_path), str(count)],
                env=env, text=True, capture_output=True, timeout=20, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            outcomes.append(json.loads(result.stdout))
        self.assertEqual(outcomes, [
            {"effects": ["notion"], "complete": False},
            {"effects": ["discord", "mark_read"], "complete": True},
        ])


if __name__ == "__main__":
    unittest.main()
