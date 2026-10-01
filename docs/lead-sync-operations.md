# Lead sync: blocked delivery, truthful failures

Issue: https://github.com/youjig45-a11y/youjig45-a11y.github.io/issues/1

## Current state

The source was inspected on 2026-10-01 UTC. The workflow schedules the script every
15 minutes, but this inspection establishes neither installed credentials nor
successful production delivery. The previous implementation ignored Discord and
Formspree mark-read HTTP failures, logged lead data/exception text, and returned
exit code zero after per-lead failures. Missing submission IDs did not stop writes.

This change makes those HTTP failures propagate, validates IDs before writes, and
logs fixed error categories only. All missing configuration and processing errors
return exit code 1. The existing Notion database properties are preserved exactly.
There are no schema, workflow, credential, account or service changes.

**The production entry point is blocked before all HTTP calls.**
`durable_delivery_contract_missing` is an intentional nonzero failure. It cannot be
cleared by an approval environment variable. Existing configured automation would
report this blocker after installation; this patch is preparation, not an activated
or completed lead-delivery system.

## Durable-delivery blocker and next owner

The repository defines no persistent submission key, exclusive delivery claim or
per-stage receipt. GitHub-hosted runners have no durable local state. A Notion page
marker or a property search followed by page creation is insufficient: two workers
can both observe no page, and page creation can succeed while its response is lost.
Discord notification can similarly be sent while the reply or checkpoint is lost.
Marking a lead read first would hide undelivered leads and is not a fix.

The site maintainer and common operations owner must select and approve a durable
contract before a separate implementation replaces the guard. Options to evaluate:

- A persistent transactional store with a unique `(form_id, submission_id)` key,
  atomic claim, cross-run worker exclusion and stage records. Persist an attempt
  before each external write. Save the Notion page reference and verified receipts
  before later stages. Never automatically replay an ambiguous create or send.
- A maintainer-approved Notion submission-key property and delivery-state records,
  together with an authoritative external claim store. A Notion key alone does not
  impose uniqueness or resolve transport uncertainty. Property/schema changes
  require separate approval and must preserve the current database data.

Both options need an approved migration/reconciliation plan for existing unread
leads that may already have pages or notifications. A marker with a scoped hashed
key can help locate results, but it is not a uniqueness guarantee. Store raw IDs and
page references only in the approved private delivery store, never in Issue logs.

Required acceptance evidence for the next implementation:

1. Concurrent workers and restarted/ephemeral runners use the same durable claim
   and cannot create a second page for a known or uncertain attempt.
2. A Discord or mark-read failure resumes from recorded state without recreating
   the Notion page. Notification uncertainty stops for reconciliation; it does not
   trigger automatic resending.
3. An ambiguous Notion/Discord write is resolved using authorized private readback
   or maintainer review. Absence of a search hit alone does not authorize a replay.
4. Only confirmed page creation, notification and mark-read receipts count as
   delivery complete. Logs stay free of lead fields, IDs, URLs and exception text.
5. Installation, real credential/schema checks, approved bounded execution and
   workflow activation have separate evidence. Passing offline tests is not proof
   of production operation.

No live readback, schema mutation, notification, workflow dispatch or activation is
authorized by this patch. Existing uncertain leads need private manual review by
the owner; rerunning this script leaves them unchanged and reports the blocker.

## Offline verification

From the repository root, with Python 3.12+ and the workflow's `requests` dependency:

```powershell
& 'D:/ai-auto-poster/.venv/Scripts/python.exe' -I -B -m unittest discover -s tests -p 'test_formspree_sync.py' -v
```

All HTTP entry points are mocked. The actual CLI is also run in isolated child
processes with synthetic environment values and an injected fake `requests` module;
the production guard and missing-config path must each exit 1 with zero HTTP calls.
Some sequence tests replace the guard **inside the test process only** to exercise
HTTP status handling and partial/ambiguous outcomes, then restore it for retry.
Those retries make no further calls because delivery is blocked; these tests do
not establish automatic recovery, durable persistence or exactly-once sending.
