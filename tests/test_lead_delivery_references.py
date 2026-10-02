"""Concrete local reference store, exercised only with temporary synthetic data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lead_delivery_adapter as adapter
import lead_delivery_state as state
import lead_delivery_references as references
from test_lead_delivery_adapter import FailingDatabase, SyntheticReceipts, effect_result, receipt_object

SENTINEL = "synthetic-private:https://private.invalid/person?key=synthetic-only"


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-real-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "references.sqlite"
        self.scope = adapter.Scope(state.digest("synthetic-lead"), "notion", "a" * 32, 1)
        self.reference = adapter.PrivateEffectReference(self.scope, SENTINEL)
        self.store = references.SQLitePrivateReferenceStore(self.path)
        self.addCleanup(self.store.close)

    def count(self):
        return self.store._db.execute("SELECT COUNT(*) FROM private_references").fetchone()[0]

    def assert_safe_failure(self, callback):
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output):
            with self.assertRaises(references.ReferenceStoreError) as caught:
                callback()
        error = caught.exception
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        for text in (str(error), rendered, output.getvalue()):
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn(str(self.path), text)
            self.assertNotIn("private.invalid", text)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(error.__suppress_context__)
        self.assertIsNone(error.__cause__)
        return str(error)

    def test_roundtrip_reopen_and_idempotent_persistence(self):
        self.assertIsNone(self.store.require_available())
        self.assertIsNone(self.store.persist(self.reference))
        self.store.persist(self.reference)
        self.assertEqual(self.count(), 1)
        self.store.close()
        with references.SQLitePrivateReferenceStore(self.path) as reopened:
            self.assertEqual(reopened.resolve(self.scope, state.digest(SENTINEL)), self.reference)

    def test_explicit_absolute_path_and_existing_parent_are_required(self):
        for candidate in (Path("relative.sqlite"), ":memory:", None,
                          self.root / "missing" / "db.sqlite", self.root / ".." / "db.sqlite"):
            with self.subTest(candidate_type=type(candidate).__name__):
                self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(candidate))
        self.assertFalse((self.root / "missing").exists())

    def test_unknown_engine_and_empty_existing_database_are_not_mutated(self):
        for name, schema in (("unknown.sqlite", "CREATE TABLE unrelated (value TEXT)"),
                             ("empty.sqlite", None), ("engine.sqlite", "engine")):
            path = self.root / name
            if schema == "engine":
                with state.DeliveryStore(path):
                    pass
            else:
                with closing(sqlite3.connect(path)) as db:
                    if schema:
                        db.execute(schema)
            before = path.read_bytes()
            files_before = {p.name for p in self.root.iterdir()}
            real_connect = sqlite3.connect
            connects = []
            def traced_connect(database, *args, **kwargs):
                connects.append(database)
                return real_connect(database, *args, **kwargs)
            with patch.object(references.sqlite3, "connect", side_effect=traced_connect):
                self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(path))
            self.assertEqual(connects, [path.as_uri() + "?mode=ro&immutable=1"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual({p.name for p in self.root.iterdir()}, files_before)

    def test_unknown_version_and_modified_schema_are_not_migrated(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 2")
        before = self.path.read_bytes()
        self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(self.path))
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 1")
            db.execute("CREATE VIEW unexpected AS SELECT value FROM private_references")
        before = self.path.read_bytes()
        self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_scope_types_and_reference_bounds_are_rejected_before_writes(self):
        cases = [replace(self.reference, scope=replace(self.scope, generation=True)),
                 replace(self.reference, scope=replace(self.scope, generation=2**63)),
                 replace(self.reference, scope=replace(self.scope, key="A" * 64)),
                 replace(self.reference, scope=replace(self.scope, attempt_token="b" * 31)),
                 replace(self.reference, scope=replace(self.scope, stage="other")),
                 replace(self.reference, value=""), replace(self.reference, value=" " * 5),
                 replace(self.reference, value="x" * 4097), replace(self.reference, value=True)]
        for reference in cases:
            with self.subTest(kind=type(reference.value).__name__):
                self.assert_safe_failure(lambda: self.store.persist(reference))
        self.assertEqual(self.count(), 0)

    def test_attempt_conflicts_preserve_original_value_and_scope(self):
        self.store.persist(self.reference)
        for changed in (replace(self.reference, value="synthetic-private:other"),
                        replace(self.reference, scope=replace(self.scope, generation=2)),
                        replace(self.reference, scope=replace(self.scope, key=state.digest("other"))),
                        replace(self.reference, scope=replace(self.scope, stage="discord"))):
            self.assertEqual(self.assert_safe_failure(lambda: self.store.persist(changed)),
                             "private_reference_conflict")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.resolve(self.scope, state.digest(SENTINEL)), self.reference)

    def test_missing_wrong_scope_digest_and_tampered_value_never_resolve(self):
        self.assert_safe_failure(lambda: self.store.resolve(self.scope, state.digest(SENTINEL)))
        self.store.persist(self.reference)
        self.assert_safe_failure(lambda: self.store.resolve(replace(self.scope, generation=True),
                                                           state.digest(SENTINEL)))
        self.assert_safe_failure(lambda: self.store.resolve(replace(self.scope, generation=2),
                                                           state.digest(SENTINEL)))
        self.assert_safe_failure(lambda: self.store.resolve(self.scope, "A" * 64))
        self.assert_safe_failure(lambda: self.store.resolve(self.scope, state.digest("other")))
        self.store._db.execute("UPDATE private_references SET value=?", ("synthetic-private:changed",))
        self.assert_safe_failure(lambda: self.store.resolve(self.scope, state.digest(SENTINEL)))

    def test_two_connection_contention_never_overwrites_attempt(self):
        barrier = threading.Barrier(2)
        def competing(value):
            with references.SQLitePrivateReferenceStore(self.path) as worker:
                barrier.wait(timeout=10)
                try:
                    worker.persist(replace(self.reference, value=value))
                    return "saved", value
                except references.ReferenceStoreError as error:
                    return str(error), None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(competing, ("synthetic-private:one", "synthetic-private:two")))
        self.assertEqual(sorted(label for label, _ in outcomes), ["private_reference_conflict", "saved"])
        winner = next(value for label, value in outcomes if label == "saved")
        self.assertEqual(self.store.resolve(self.scope, state.digest(winner)).value, winner)
        self.assertEqual(self.count(), 1)

    def test_commit_failure_rolls_back_and_suppresses_database_exception(self):
        original = self.store._db
        self.store._db = FailingDatabase(original, "COMMIT")
        self.assert_safe_failure(lambda: self.store.persist(self.reference))
        self.assertFalse(original.in_transaction)
        self.assertEqual(self.count(), 0)
        self.store._db = original
        self.store.persist(self.reference)
        self.assertEqual(self.count(), 1)

    def test_schema_change_before_begin_rejects_before_row_commit(self):
        original = self.store._db
        foreign_bytes = []
        path = self.path
        class ChangedBeforeBegin:
            def __getattr__(self, name):
                return getattr(original, name)
            def execute(self, sql, *args):
                if sql == "BEGIN IMMEDIATE" and not foreign_bytes:
                    with closing(sqlite3.connect(path)) as foreign:
                        foreign.execute("CREATE TABLE unrelated (value TEXT)")
                    foreign_bytes.append(path.read_bytes())
                return original.execute(sql, *args)
        self.store._db = ChangedBeforeBegin()
        self.assertEqual(self.assert_safe_failure(lambda: self.store.persist(self.reference)),
                         "reference_schema_unsupported")
        self.assertFalse(original.in_transaction)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.path.read_bytes(), foreign_bytes[0])

    def test_reserved_device_components_are_rejected_before_all_io(self):
        for candidate in (self.root / "NUL", self.root / "nul.txt", self.root / "CONIN$",
                          self.root / "COM1.log", self.root / "COM\u00b9", self.root / "LPT\u00b2.txt",
                          self.root / "AUX" / "db.sqlite"):
            with self.subTest(candidate_name=candidate.name):
                with patch.object(references, "_fingerprint", side_effect=AssertionError(SENTINEL)) as metadata, \
                        patch.object(references.os, "open", side_effect=AssertionError(SENTINEL)) as opened, \
                        patch.object(references.sqlite3, "connect", side_effect=AssertionError(SENTINEL)) as connected:
                    self.assertEqual(self.assert_safe_failure(
                        lambda: references.SQLitePrivateReferenceStore(candidate)), "reference_path_invalid")
                    self.assertEqual(metadata.call_count + opened.call_count + connected.call_count, 0)

    def test_closed_store_and_private_exception_text_are_sanitized(self):
        self.store.close()
        self.assert_safe_failure(self.store.require_available)
        self.assert_safe_failure(lambda: self.store.persist(self.reference))
        with patch.object(references.sqlite3, "connect", side_effect=sqlite3.OperationalError(SENTINEL)):
            self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(self.root / "fail.sqlite"))

    def test_parent_reparse_or_symlink_is_rejected_before_creation(self):
        target, link = self.root / "target", self.root / "link"
        target.mkdir()
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                                    env={"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")},
                                    capture_output=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(target, target_is_directory=True)
        try:
            self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(link / "db.sqlite"))
            self.assertFalse((target / "db.sqlite").exists())
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()

    def test_hardlinked_database_is_rejected_without_mutation(self):
        self.store.close()
        alias = self.root / "alias.sqlite"
        os.link(self.path, alias)
        before = self.path.read_bytes()
        self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(alias))
        self.assertEqual(self.path.read_bytes(), before)
        alias.unlink()

    def test_database_replacement_is_rejected_before_another_write(self):
        moved = self.root / "moved.sqlite"
        self.store.close()
        # Reopen after replacing the path with a new valid store to establish a
        # reproducible mismatch without renaming an open Windows database handle.
        old_identity = self.store._file
        self.path.rename(moved)
        with references.SQLitePrivateReferenceStore(self.path) as replacement:
            replacement._file = old_identity
            self.assert_safe_failure(lambda: replacement.persist(self.reference))
        with references.SQLitePrivateReferenceStore(self.path) as check:
            self.assertEqual(check._db.execute("SELECT COUNT(*) FROM private_references").fetchone()[0], 0)

    def test_database_swap_after_readonly_preflight_does_not_mutate_unknown_database(self):
        self.store.close()
        original_connect = sqlite3.connect
        foreign_before = []
        statements = []
        def swapped_connect(database, *args, **kwargs):
            if database == self.path.as_uri() + "?mode=rw":
                self.path.rename(self.root / "original.sqlite")
                with closing(original_connect(self.path)) as foreign:
                    foreign.execute("CREATE TABLE unrelated (value TEXT)")
                foreign_before.append(self.path.read_bytes())
                connection = original_connect(database, *args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection
            return original_connect(database, *args, **kwargs)
        with patch.object(references.sqlite3, "connect", side_effect=swapped_connect):
            self.assertEqual(self.assert_safe_failure(
                lambda: references.SQLitePrivateReferenceStore(self.path)), "reference_path_changed")
        self.assertEqual(self.path.read_bytes(), foreign_before[0])
        self.assertEqual(statements, [])

    def test_missing_confirmed_concrete_reference_blocks_followon_before_intent(self):
        with adapter.DeliveryAdapter(self.root / "engine.sqlite", references=self.store,
                                     receipts=SyntheticReceipts(), clock=lambda:100) as worker:
            key = worker.store.enroll("synthetic", "lost-confirmed-reference")
            claim = worker.store.claim(key)
            worker.advance(claim, effect_result)
            worker.store.release(claim)
            self.store._db.execute("DELETE FROM private_references")
            claim = worker.store.claim(key)
            calls = []
            with self.assertRaisesRegex(adapter.AdapterError, "^private_reference_unavailable$"):
                worker.advance(claim, lambda attempt: calls.append(attempt))
            self.assertEqual(calls, [])
            self.assertEqual(worker.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            self.assertEqual(worker.store.snapshot(key)["stages"],
                             {"notion":"confirmed","discord":"pending","mark_read":"pending"})
            worker.store.release(claim)

    def test_new_reserved_file_filled_by_other_writer_is_rejected_before_begin(self):
        path = self.root / "new-race.sqlite"
        original_connect = sqlite3.connect
        foreign_before = []
        statements = []
        def filled_connect(database, *args, **kwargs):
            if database == path.as_uri() + "?mode=rw":
                with closing(original_connect(path)) as foreign:
                    foreign.execute("CREATE TABLE unrelated (value TEXT)")
                foreign_before.append(path.read_bytes())
                connection = original_connect(database, *args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection
            return original_connect(database, *args, **kwargs)
        with patch.object(references.sqlite3, "connect", side_effect=filled_connect):
            self.assertEqual(self.assert_safe_failure(lambda: references.SQLitePrivateReferenceStore(path)),
                             "reference_schema_unsupported")
        self.assertEqual(path.read_bytes(), foreign_before[0])
        self.assertEqual(statements, ["SELECT name FROM sqlite_master"])

    def test_concrete_store_does_not_supply_receipt_or_operator_authority(self):
        with adapter.DeliveryAdapter(self.root / "engine.sqlite", references=self.store,
                                     clock=lambda: 100) as worker:
            key = worker.store.enroll("synthetic", "default-deny")
            claim = worker.store.claim(key)
            calls = []
            with self.assertRaises(adapter.AdapterError):
                worker.advance(claim, lambda attempt: calls.append(attempt))
            self.assertEqual(calls, [])
            self.assertEqual(worker.store.snapshot(key)["stages"]["notion"], "pending")

    def abrupt_case(self, cutpoint, expected_count, *, confirmed=False):
        runner = r'''
import os, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from lead_delivery_references import SQLitePrivateReferenceStore
from lead_delivery_adapter import DeliveryAdapter
from test_lead_delivery_adapter import SyntheticReceipts, effect_result
cutpoint = sys.argv[3]
engine, private, effects = map(Path, sys.argv[4:7])
class AbruptStore(SQLitePrivateReferenceStore):
    def persist(self, reference):
        if cutpoint == "before_private_commit": os._exit(86)
        super().persist(reference)
        if cutpoint == "after_private_commit": os._exit(86)
refs = AbruptStore(private)
worker = DeliveryAdapter(engine, references=refs, receipts=SyntheticReceipts(), clock=lambda:100)
key = worker.store.enroll("synthetic-concrete", "crash")
claim = worker.store.claim(key, lease_seconds=60)
if cutpoint == "after_engine_confirm":
    original = worker.store.confirm
    def abrupt(*args):
        original(*args)
        os._exit(86)
    worker.store.confirm = abrupt
def fake(attempt):
    assert attempt.stage == "notion"
    with effects.open("a",encoding="utf8") as output:
        output.write("notion\n")
        output.flush()
        os.fsync(output.fileno())
    return effect_result(attempt)
worker.advance(claim,fake)
raise AssertionError("cutpoint_not_reached")
'''
        engine, private, effects = (self.root / name for name in
                                    ("crash-engine.sqlite", "crash-private.sqlite", "effects.txt"))
        result = subprocess.run([sys.executable, "-I", "-B", "-c", runner, str(SCRIPTS),
                                 str(Path(__file__).resolve().parent), cutpoint,
                                 str(engine), str(private), str(effects)],
                                env={"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")},
                                text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        key = state.lead_key("synthetic-concrete", "crash")
        with references.SQLitePrivateReferenceStore(private) as refs:
            with adapter.DeliveryAdapter(engine, references=refs, receipts=SyntheticReceipts(),
                                         clock=lambda:120) as worker:
                with self.assertRaises(state.Busy):
                    worker.store.claim(key)
            with adapter.DeliveryAdapter(engine, references=refs, receipts=SyntheticReceipts(),
                                         clock=lambda:161) as worker:
                if confirmed:
                    claim = worker.store.claim(key)
                    calls = []
                    def fake_next(attempt):
                        self.assertEqual(worker.resolve_confirmed(key,"notion").value,
                                         "synthetic-private:notion")
                        calls.append(attempt.stage)
                        return effect_result(attempt)
                    worker.advance(claim, fake_next)
                    worker.advance(claim, fake_next)
                    self.assertEqual(calls, ["discord", "mark_read"])
                    self.assertTrue(worker.store.snapshot(key)["complete"])
                    worker.store.release(claim)
                else:
                    with self.assertRaises(state.OutcomeUncertain):
                        worker.store.claim(key)
                    self.assertEqual(worker.store.snapshot(key)["stages"],
                                     {"notion":"uncertain","discord":"pending","mark_read":"pending"})
                    row = worker.store.db.execute("SELECT token,generation FROM attempts").fetchone()
                    scope = adapter.Scope(key,"notion",row[0],row[1])
                    candidate = {"version":1,"scope":vars(scope),"decision":"confirmed",
                                 "authority_digest":state.digest("synthetic-operator"),
                                 "evidence_digest":state.digest("synthetic-review"),
                                 "drain_evidence_digest":None,
                                 "receipt":receipt_object(scope,"synthetic-private:notion")}
                    import json
                    with self.assertRaisesRegex(adapter.AdapterError,"^operator_authorization_required$"):
                        worker.reconcile(json.dumps(candidate))
                self.assertEqual(refs._db.execute("SELECT COUNT(*) FROM private_references").fetchone()[0],
                                 expected_count)
        self.assertEqual(effects.read_text(encoding="utf8").splitlines(), ["notion"])

    def test_concrete_abrupt_exit_before_private_commit_never_replays(self):
        self.abrupt_case("before_private_commit", 0)

    def test_concrete_abrupt_exit_after_private_commit_retains_uncertainty(self):
        self.abrupt_case("after_private_commit", 1)

    def test_concrete_abrupt_exit_after_engine_confirm_resumes_without_notion(self):
        self.abrupt_case("after_engine_confirm", 3, confirmed=True)


if __name__ == "__main__":
    unittest.main()
