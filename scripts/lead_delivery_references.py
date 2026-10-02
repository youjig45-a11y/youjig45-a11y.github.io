"""Explicit local SQLite reference storage; no transport or production wiring."""

from functools import wraps
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import stat

from lead_delivery_adapter import (
    PrivateEffectReference, PrivateReferenceStore, Scope, _reference, _validate_scope,
)
from lead_delivery_state import digest


class ReferenceStoreError(Exception):
    """Fixed categories only; no paths, references or database exception text."""


_CATEGORIES = frozenset({
    "reference_path_invalid", "reference_path_changed", "reference_schema_unsupported",
    "private_reference_invalid", "private_reference_unavailable", "private_reference_conflict",
    "reference_store_unavailable", "reference_store_initialization_failed",
    "reference_store_persistence_failed", "reference_store_resolution_failed",
    "reference_store_close_failed",
})


def _deny(category):
    raise ReferenceStoreError(category) from None


def _boundary(category):
    def decorate(method):
        @wraps(method)
        def checked(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except Exception as error:
                if (type(error) is ReferenceStoreError and len(error.args) == 1
                        and type(error.args[0]) is str and error.args[0] in _CATEGORIES):
                    raise ReferenceStoreError(error.args[0]) from None
                _deny(category)
        return checked
    return decorate


_SCHEMA = """CREATE TABLE private_references (
    attempt_token TEXT PRIMARY KEY NOT NULL,
    key TEXT NOT NULL,
    stage TEXT NOT NULL,
    generation INTEGER NOT NULL,
    effect_digest TEXT NOT NULL,
    value TEXT NOT NULL
)"""


def _fingerprint(path, *, database=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        _deny("reference_path_invalid")
    if database:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _deny("reference_path_invalid")
    elif not stat.S_ISDIR(info.st_mode):
        _deny("reference_path_invalid")
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _scope_checked(scope):
    _validate_scope(scope)
    # SQLite INTEGER cannot represent arbitrary Python integers.
    if scope.generation > 2**63 - 1:
        _deny("private_reference_invalid")


def _digest_checked(value):
    if (type(value) is not str or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        _deny("private_reference_invalid")


class SQLitePrivateReferenceStore(PrivateReferenceStore):
    """Same-host preparation only. Explicit paths do not establish privacy policy."""

    @_boundary("reference_store_initialization_failed")
    def __init__(self, path: Path):
        if not isinstance(path, (str, Path)):
            _deny("reference_path_invalid")
        path = Path(path)
        if (not path.is_absolute() or ".." in path.parts
                or path.drive.startswith("\\")
                or any(PureWindowsPath(part).is_reserved() for part in path.parts)
                or any(":" in part or part.rstrip(" .") != part
                       for part in path.parts[1:])):
            _deny("reference_path_invalid")
        self._path = path
        self._parents = {parent: _fingerprint(parent) for parent in reversed(path.parents)}
        self._db = None
        try:
            try:
                self._file = _fingerprint(path, database=True)
            except FileNotFoundError:
                self._check_parents()
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(descriptor)
                self._file = _fingerprint(path, database=True)
                new = True
            else:
                # Immutable read-only preflight avoids unknown-DB WAL/SHM side effects.
                probe = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    self._check_schema(probe)
                finally:
                    probe.close()
                new = False
            self._check_identity()
            self._db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                       timeout=5, isolation_level=None)
            self._check_identity()
            if new:
                if (self._db.execute("SELECT name FROM sqlite_master").fetchall()
                        or self._db.execute("PRAGMA user_version").fetchone()[0] != 0):
                    _deny("reference_schema_unsupported")
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    if (self._db.execute("SELECT name FROM sqlite_master").fetchall()
                            or self._db.execute("PRAGMA user_version").fetchone()[0] != 0):
                        _deny("reference_schema_unsupported")
                    self._db.execute(_SCHEMA)
                    self._db.execute("PRAGMA user_version = 1")
                    self._db.execute("COMMIT")
                except BaseException:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    raise
            else:
                # Recheck the opened handle before any mutating statement/PRAGMA.
                self._check_schema(self._db)
            self._db.execute("PRAGMA synchronous = FULL")
        except BaseException:
            if self._db is not None:
                self._db.close()
                self._db = None
            raise

    @staticmethod
    def _check_schema(database):
        objects = database.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if objects != [("private_references", "table", _SCHEMA)]:
            _deny("reference_schema_unsupported")
        if database.execute("PRAGMA user_version").fetchone()[0] != 1:
            _deny("reference_schema_unsupported")

    def _check_parents(self):
        for parent, identity in self._parents.items():
            if _fingerprint(parent) != identity:
                _deny("reference_path_changed")

    def _check_identity(self):
        self._check_parents()
        if _fingerprint(self._path, database=True) != self._file:
            _deny("reference_path_changed")

    @_boundary("reference_store_unavailable")
    def require_available(self):
        if self._db is None:
            _deny("reference_store_unavailable")
        self._check_identity()
        self._check_schema(self._db)

    @_boundary("reference_store_persistence_failed")
    def persist(self, reference: PrivateEffectReference):
        if type(reference) is not PrivateEffectReference:
            _deny("private_reference_invalid")
        _scope_checked(reference.scope)
        effect_digest = digest(reference.value)
        _reference(reference, reference.scope, effect_digest)
        self.require_available()
        scope = reference.scope
        expected = (scope.attempt_token, scope.key, scope.stage, scope.generation,
                    effect_digest, reference.value)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._check_identity()
            self._check_schema(self._db)
            existing = self._db.execute(
                "SELECT attempt_token,key,stage,generation,effect_digest,value "
                "FROM private_references WHERE attempt_token=?", (scope.attempt_token,),
            ).fetchone()
            if existing is None:
                self._db.execute("INSERT INTO private_references VALUES(?,?,?,?,?,?)", expected)
            elif existing != expected:
                _deny("private_reference_conflict")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        if self.resolve(scope, effect_digest) != reference:
            _deny("private_reference_unavailable")

    @_boundary("reference_store_resolution_failed")
    def resolve(self, scope: Scope, effect_digest: str):
        _scope_checked(scope)
        _digest_checked(effect_digest)
        self.require_available()
        row = self._db.execute(
            "SELECT attempt_token,key,stage,generation,effect_digest,value "
            "FROM private_references WHERE attempt_token=?", (scope.attempt_token,),
        ).fetchone()
        if row is None or row[:5] != (scope.attempt_token, scope.key, scope.stage,
                                     scope.generation, effect_digest):
            _deny("private_reference_unavailable")
        reference = PrivateEffectReference(scope, row[5])
        return _reference(reference, scope, effect_digest)

    @_boundary("reference_store_close_failed")
    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
