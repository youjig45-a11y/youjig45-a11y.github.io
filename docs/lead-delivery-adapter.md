# Transportless lead delivery adapter preparation

Tracks [site Issue #1](https://github.com/youjig45-a11y/youjig45-a11y.github.io/issues/1).
Base: PR3 c4da3dfea4c5faa2a457b9cdf6ea70e400a4faea. This prototype adds only
the adapter, its synthetic tests and this document. The existing state engine,
production entry point and workflows are unchanged. Production remains blocked.

## What this module does

scripts/lead_delivery_adapter.py has no HTTP transport, credential loading,
environment enable flag, production CLI or default store location. Its caller
supplies an absolute temporary path to the existing SQLite prototype. This is
not a selection of the production backend or runner.

ReceiptVerifier, PrivateReferenceStore and AuthorityVerifier are abstract
boundaries. Missing implementations deny by default. Explicitly injected Python
objects are trusted-code boundaries, not authenticated operators or services.
Returning a VerifiedReceipt or VerifiedAuthority object proves no real identity
by itself. The supplied test implementations authenticate only their known
synthetic fixtures. They must never be installed as production verifiers.

The internal candidate JSON format is version 1. It is a local parsing contract,
not an adopted Formspree, Notion, Discord or remote-store schema:

- Receipt: version, scope, evidence_digest, effect_digest.
- Scope: key, stage, attempt_token, generation. These match the persisted engine
  attempt exactly. Tokens are the engine's lowercase 32-character hex tokens;
  generation is a positive integer, with booleans and coercion rejected.
- Reconciliation: version, scope, decision, authority_digest, evidence_digest,
  drain_evidence_digest, receipt. Confirmed requires a same-scope receipt and
  null drain evidence. No_effect requires null receipt and a drain evidence
  digest, whose authenticity still requires the trusted authority verifier.

Unknown/missing/duplicate fields, unknown versions, trailing JSON, oversized
input, malformed digests and scope changes fail closed. Input fields such as
approved, authorized, verified, fresh and complete cannot grant authority.
Nested scopes in verifier/reference return objects receive the same exact-type
validation; Python's equality between True and 1 cannot establish scope binding.
Candidate and reference representations hide field values. Errors contain only
fixed categories; private values, URLs and injected exception text are not logged.

Adapter-owned public methods also sanitize ordinary clock/database exceptions,
including constructor, close, resolver, advancement and reconciliation failures.
Only exact known engine error types with allowlisted fixed categories retain their
semantic type; other exceptions become a fixed adapter failure category. Recreated
exceptions use from None so standard formatted tracebacks suppress the original
private exception chain. Direct calls to the exposed engine or injected objects
retain those objects' own behavior. Interrupt/exit signals are not normalized.
Reconciliation still clears its scoped permit in a finally block on every exit.

## Ordering and failure behavior

Default-denied components stop advancement before a new intent or callback.
For a configured synthetic adapter, the engine commits the attempt before the
callback. The adapter then checks exact scope, validates the private effect
reference digest, asks the receipt verifier for exact scoped evidence, persists
the private reference, and resolves that persisted reference before returning a
receipt to the engine. Only then may the engine confirm that stage.

If an effect might have happened but parsing, verification, reference persistence,
readback, receipt recording or lease validity fails, the engine retains uncertainty.
An expired in-flight attempt may remain inflight until the next claim converts it
to uncertain. A successor cannot replay it. Local fencing cannot cancel an already
running remote write or eliminate the gap between checking a lease and dispatch.

Before later effects, each previously confirmed stage must still have its exact
private reference and verified readback. Missing references or malformed stored
digests stop before the next callback. The engine itself stores only correlatable
hashes, not recoverable private identities.

The reference and state stores are not one atomic transaction. A crash can leave a
persisted reference with an uncertain engine attempt; that is a reconciliation
case, not replay permission. The synthetic two-store tests prove local ordering
and graceful restart behavior, not production durability or power-loss guarantees.

## Reconciliation boundary

The adapter binds the candidate's generation to the persisted attempt, because
the existing Reconciliation object has no generation field. Authority verification
must be a read-only, effect-free operation: the engine checks current state after
its authorizer is called, so a prior authority result cannot prove that a claim is
still available.

Only after exact authority/readback verification does the adapter create one
in-process permit for the exact engine request object. It clears that permit in
a finally block. The engine's injected authorizer rejects other requests and
direct calls. This bridge uses the engine's existing injection boundary; it is
not an operator authentication system. Adapter instances are single-worker,
single-thread objects using the engine's same-thread SQLite connection.

No_effect requires trusted evidence that the old executor is stopped/drained
and cannot produce a late effect. A digest, expired lease, absent search hit or
self-approved JSON is insufficient. A confirmed decision additionally needs
the private reference and receipt verifier. Repeated/stale reconciliation and
live claim theft are rejected by the unchanged engine.

Historical unknown leads cannot provide receipt or authority input. Migration
preview stays planning-only. Calling the engine's enroll method is not proof that
a real historical lead is safe for new dispatch; production enrollment policy
remains unimplemented.

## Offline validation

From the isolated repository root, with the approved existing interpreter:

    & 'D:/ai-auto-poster/.venv/Scripts/python.exe' -I -B -m unittest discover -s tests -p 'test_*.py' -v

Tests use temporary databases, synthetic identities, fake effects and fixture
verifiers. They cover field/type/scope substitution, false authority, private
reference loss, post-effect persistence/verification/confirmation failures,
lease expiry, default denial, receipt adoption and no_effect restrictions.

The two-process test reopens the same temporary engine and synthetic reference
databases. Process one records only fake Notion. Process two resolves that private
synthetic identity and runs only fake Discord/mark_read. Completion requires all
three confirmed stages. Neither this output nor passing tests establishes actual
Notion/Discord/Formspree delivery, operator authority or scheduled operation.

## Remaining production inputs and owner

The site maintainer and common operations owner still must select the private
durable location and responsible runner, shared-host/DB scope, access controls,
backup/recovery, clock and failure ownership. GitHub-hosted ephemeral SQLite is
not a durable deployment.

The transport/private-store owner must supply real receipt semantics, trusted
verification/readback, recoverable immutable effect references and reviewed
cross-store failure behavior. The maintainer must supply real authority, expiry,
revocation/replay and old-executor drain contracts, plus an authorized private
review plan for uncertain historical leads. No new remote schema, signing
authority or service contract is selected here.

Production integration, credentials, migration execution, workflow activation
and bounded live delivery require their own implementation, review and applicable
authorization. There is no exactly-once guarantee. Keep Issue #1 open.
