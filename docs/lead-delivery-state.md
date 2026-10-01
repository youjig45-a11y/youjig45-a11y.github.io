# Isolated lead delivery state prototype

Tracks [site Issue #1](https://github.com/youjig45-a11y/youjig45-a11y.github.io/issues/1)
and [console Issue #13](https://github.com/youjig45-a11y/agent-team-console/issues/13).
This local proof builds on the blocked entry point in site PR #2. It does not
modify `formspree_sync.py`, the workflow, credentials, remote schemas or real leads.

## What exists

`scripts/lead_delivery_state.py` is a standard-library SQLite state engine. It has
no HTTP transport, environment activation flag, production CLI integration or
default store path. Callers must supply an absolute path. Verification uses only
temporary databases, synthetic identifiers and fake executors.

Each lead uses a SHA-256 key over the JSON pair `(form_id, submission_id)`. Raw
identifiers and lead fields are not persisted. Keys and receipt digests are
correlatable metadata, not anonymization; a future store still needs a private
approved location and access controls. No such controls are configured here.

An immediate SQLite transaction serializes claims for that key. Each claim has a
new owner token, increasing generation and lease expiry. Another live worker is
rejected. An idle expired claim can be replaced; its old owner cannot begin or
confirm an attempt. An expired **in-flight** attempt becomes uncertain and blocks
successor claims rather than being replayed. This also covers a process crash
after persisting intent but before the receipt.

Stages run in order: `notion`, `discord`, `mark_read`. Before the fake executor is
called, its attempt and in-flight status have been committed. The engine rechecks
the claim immediately before invoking it. A confirmed stage requires a scoped
receipt with evidence and effect-reference digests; the confirmed receipts survive
reopening the same database. Later stages resume without repeating confirmed
stages. Completion requires all three confirmed receipts.

The engine does **not** verify remote receipt authenticity. A future transport
adapter must perform that verification before calling `confirm`. A caller can
construct synthetic receipts in these tests, so these records establish local
state behavior, not live delivery. The prototype retains only digests; it cannot
recover a real Notion page URL from them. An approved private reference store or
equivalent durable adapter is required before real recovery can use page identity.

## Failures, fencing and reconciliation

Any executor exception, malformed receipt or failed/late receipt recording is
conservatively uncertain. Exception text is not saved. The engine does not infer
that an HTTP rejection or timeout means no remote effect. Releasing an unfinished
attempt similarly records uncertainty. There is no automatic replay of uncertain
attempts, and no claim of exactly-once delivery.

Local fencing controls state changes and whether a known expired worker starts a
new callback. It cannot cancel an external write already in progress, nor close
the scheduling gap between a local check and an external API call. A late old
worker may still cause an external effect. An unresolved persisted attempt means
the successor also cannot send. Tests explicitly exercise this limitation and
reject the stale worker's confirmation without allowing a successor replay.

`reconcile` defaults to rejection. No operator authorization implementation is
provided. A future adapter must verify an exact operator authority record and
private readback, bound to the lead key, stage, attempt token and decision. An
injected authorizer is the prototype's boundary; a digest or a callback returning
true is not proof of actual authority. Tests inject synthetic authorization only.

After verified reconciliation, the engine can adopt a confirmed effect receipt or
record a confirmed `no_effect` decision and permit a new attempt. It rejects live
claim theft, mismatched attempt tokens and repeated reconciliation. Both decisions
retain the old attempt and the authority/evidence references for audit.
Before a real `no_effect` decision, the operator must establish that the old
executor is stopped/drained and cannot produce a late effect; an absent search
result or expired local lease is insufficient. This operator evidence contract
remains unimplemented, so production reconciliation remains blocked.

`preview_migration` returns deduplicated hashed keys with `status: unknown` and
`action: review_existing_effects`. It has no store access and never seeds confirmed
stages. It is a synthetic planning preview, not permission to enroll historical
unread submissions as fresh deliveries. Real migration requires private review of
possible existing Notion pages and Discord messages before any attempt is allowed.

## Offline proof

From the repository root:

```powershell
& 'D:/ai-auto-poster/.venv/Scripts/python.exe' -I -B -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover six simultaneous workers, committed intent visible to a second
connection, stale owner/generation fencing, idle and in-flight lease expiry,
crash/restart, a late external effect, per-stage receipt checks, Discord failure,
timeout, receipt-recording failure, default-denied and synthetic-authorized
reconciliation, migration preview and completion only after all stage receipts.

The actual engine is also executed in two separate Python child processes using
one temporary database and fake callbacks. Phase one records only `notion` and
reports `complete: false`; phase two reopens that database, executes only `discord`
and `mark_read`, and reports `complete: true`. This proves restart/resume for that
local synthetic store. The unchanged production CLI remains blocked before HTTP.

## Minimum production decisions and remaining work

1. The owner must approve the private durable store location, backup/recovery
   policy, filesystem suitability, time source and responsible executor. SQLite
   on a temporary GitHub-hosted runner is **not** a durable production store. This
   implementation assumes all workers share the same healthy local database; a
   multi-host design needs a proven transactional shared store instead.
2. Approve the existing uncertain-lead reconciliation/migration plan, including
   stopping/draining old executors and verifying real effects privately. Do not
   treat unread status, absent search hits or unknown history as permission to send.
3. Implement and review the receipt-verifying transport, private effect-reference
   storage, operator authorization and failure/readback adapters. Reject unknown
   schema versions; this prototype performs no schema migration. Database loss,
   tampering, disk failure, clock rollback and real service behavior are not
   validated production guarantees.
4. Only after those contracts are implemented and independently verified should a
   separately approved change integrate or replace the current production guard.
   Installation, workflow activation and bounded live execution need their own
   authorization/evidence. Offline completion never promotes scheduled/live state.
