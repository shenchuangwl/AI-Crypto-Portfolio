# OnlyCoin source-only control and observer contract

This implements the source/observer boundary in Astra6 OnlyCoin §§8/11, **not a real DMR strategy or trading chain**. `dmr_executable=false`, `order_instruction=false`, `eligibility_only=true` remain immutable protocol assertions. No trading libraries, account credentials, third-party strategy imports, orders, cancellation, flattening, or changes to main/X/Y accepted datasets occur.

## Gateway integration

Import from `coin_selection.onlycoin_source` (add `services/coin-selection/src` to Python import path):

- `source_status(root=ROOT) -> dict`: desired `enabled`, monotonic `revision` and `generation`, `consumer_connected`, `consumer_kind=local_observer`, `consumer_id`, `trading_connected=false`, `dmr_executable=false`, `effective_state`, `disable_pending`, latest `snapshot_revision`, and `accepted_revision`.
- `set_source_control(payload, *, root=ROOT, actor='unspecified') -> dict`: persistent control receipt (`enabled`, `revision`, `generation`, `replay_policy`). Gateway must pass its authenticated actor explicitly. Required payload: `enabled` (strict JSON boolean), `expected_revision` (strict nonnegative integer; bool forbidden), `request_id` (nonblank string). Optional `reason` string and `replay_policy`, whose only allowed value is `current_day_snapshot`; omitted policy is normalized in receipt. Unknown keys rejected. Every accepted new request advances revision and generation, even same-state commands; an exact repeated request returns the original receipt, not current status. Reused ID with different payload and stale CAS raise `SourceConflict(ValueError)`; other invalid inputs raise `ValueError`. Replays are checked before CAS, persist across process restart, and do not create duplicate audit entries.
- `get_source_snapshot(*, root=ROOT, now=None) -> dict`: reads `OnlyCoinLedger(root/data/coin-selection-y/review/onlycoin.sqlite, readonly=True).daily(now=...)`; writes only the separate source outbox. Returns atomic full replacement `enabled/day/generation/members` plus protocol fields described below. OFF never opens/creates the historical ledger.

Suggested authenticated route: `PUT /api/v1/onlycoin/sources/screener-y/control`. Gateway must require its configured `ONLYCOIN_ADMIN_TOKEN` bearer credential **before** mutation, fail closed if unset, compare tokens safely, and set `actor`. This module does not inspect environment secrets or implement authentication. Map malformed payload to 400 and `SourceConflict` to 409 (or 412 for an If-Match contract). If gateway exposes `If-Match`, explicitly translate it into `expected_revision` and reject disagreement; do not silently overwrite conflicting values. Return the control receipt and separately fetched status if desired; an idempotent receipt may describe an earlier generation. Browser cookie authentication, if added later, requires independent Origin/CSRF protections. Do not expose the local ACK function as an unauthenticated network endpoint.

`ROOT` resolves to the repository root. Pass an explicit disposable root in all tests. APIs accepting `now` accept timezone-aware `datetime` or ISO-8601 string, including `Z`, normalize to UTC, and reject naive timestamps. `source_status` uses real UTC time.

## Independent durable control store

All source writes reside in `data/coin-selection-y/onlycoin-source/control.sqlite`: control row, request/actor/time audit, atomic snapshot outbox, observer namespace registry, exact ACK log and heartbeat. SQLite `BEGIN IMMEDIATE` serializes control, publication and observer application. This database must **never be restored with historical review data**; back it up and preserve OFF/generation tombstones separately. Restoring an older copy of this control database itself is not supported: no local database can preserve monotonicity if an operator replaces it with its own older bytes.

Outbox `snapshot_revision`/`sequence` is independent of historical `projection_revision`; restoring/rebuilding a review ledger cannot roll back the source cursor or control generation. Full snapshots preserve member evidence objects unchanged, including backend-provided identity/provenance. A canonical SHA-256 digest identifies unchanged snapshot content, but is not authentication. Backend reads are closed after projection. Missing, invalid, stale or wrong-day projections fail closed to empty members. UTC next-midnight is the hard expiry. Availability/coverage is passed through; source-only observation does not certify trading eligibility.

## Local observer protocol

- `bridge_once(*, root=ROOT, now=None, before_apply=None) -> dict`: produces latest full snapshot and reconciles the **single fixed local observer**. Returns `delivery_state` (`acked`, `waiting_retry`, `retry`, `fenced`), `snapshot`, and on success `ack`. `before_apply` is a failure-injection test hook, not a trading callback or external transport.
- `ack_source_snapshot(snapshot, *, root=ROOT, now=None) -> dict`: applies the exact latest stored snapshot and ACKs in the same control transaction. ACK contains `consumer_id=local-observer`, exact `delivery_id`, `generation`, `accepted_revision` (source snapshot revision, not ledger revision), and `applied_count`. Rejects altered snapshots, old control generations, nonlatest revisions, expired or wrong-day messages. Repeating an exact valid snapshot is idempotent.
- `observer_members(*, root=ROOT, now=None) -> list[dict]`: authoritative observer eligibility read; enforces OFF, generation, latest-ACK and day expiry. Direct table reads bypass the eligibility contract and must not drive decisions. Expired OnlyCoin rows are lazily deleted on read; a stopped process cannot physically delete rows at midnight, but they are ineligible at midnight even while disconnected.

Delivery is **at least once with idempotent atomic application**, not network exactly-once. Stable delivery IDs survive retries/restarts. Failed application retains pending outbox with attempts, sanitized error class and durable next retry time. Exponential retry delay is bounded with deterministic digest jitter; attempt 10 onward emits `alert=true`, with continued retries rather than abandoning revocation. New full snapshots supersede/fence older pending snapshots, so no incremental gap can occur. OFF immediately advances generation and fences pending work, preventing late in-flight apply even before OFF ACK. Reopening republishes the current day's complete membership under the new generation. New days replace the prior namespace with an empty or fresh current-day snapshot; no expired historical replay. Full snapshots are used instead of separate MEMBER_ADD/DAY_EXPIRED/HEARTBEAT message types.

`ON` means only that a fresh local-observer heartbeat (60 seconds), exact snapshot ACK, and valid nonstale snapshot exist. It never means trading is connected. With no observer, an ON request remains `ENABLING`; stale projection, retry errors, expiry or a timed-out established observer yields `DEGRADED`. OFF with an earlier observer ACK is `DISABLING`/`disable_pending=true` until exact revocation ACK. Initial OFF without an observer is OFF. The previous registry may remain on disk while disable is pending, but fenced reads reject it immediately. Replacement/deletion targets only `source_id=screener-y.onlycoin`; unrelated namespaces survive. Positions and open orders are completely outside this module.

## CLI and verification

Explicit root and mode are mandatory:

```sh
python scripts/onlycoin_source_bridge.py --root /tmp/onlycoin-disposable-root --once
python scripts/onlycoin_source_bridge.py --root /tmp/onlycoin-disposable-root --watch --interval 5
```

The CLI never changes desired control and does not start a strategy/service. Watch interval must be `(0,30]` seconds; stdout is JSON receipts, once returns zero only after ACK. Do not point it at production without separately approved source deployment. A hard unavailable control database fails visibly rather than falsely reporting success. This is a single co-located observer protocol, not remote/multi-consumer replication; those require a separately designed authenticated transport and per-consumer ACK policy.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=services/coin-selection/src python -m pytest services/coin-selection/tests/test_onlycoin_source.py -q -p no:cacheprovider
```

Tests use disposable roots, read-only backend-boundary fixtures plus a real empty OnlyCoin ledger, and CLI subprocess restarts. Schema: `contracts/json-schema/onlycoin-source.schema.json`; `$defs/control` and `$defs/ack` describe mutation and local receipt shapes. Runtime code is stdlib-only; tests use pytest/jsonschema.
