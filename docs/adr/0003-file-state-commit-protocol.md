# ADR 0003: File State Store commit and recovery protocol

- Status: Accepted
- Date: 2026-08-20

## Decision

Each mutable object is persisted as a revisioned wrapper around a frozen v1
contract payload. The wrapper is internal; API serialization validates and
returns only its payload.

Mutation order is:

1. acquire the smallest applicable file lock;
2. compare `expected_revision` with the latest committed revision;
3. append and `fsync` a checksummed event containing the complete resulting
   snapshot, actor and command; the authoritative Task or Plan event also
   contains the request digest and exact command result;
4. atomically write the snapshot with a same-directory temporary file,
   `fsync`, `rename` and directory `fsync`;
5. update a rebuildable index;
6. materialize the rebuildable idempotency-result projection.

The event is the commit record. A task-only command commits its business
snapshot and result in the same Task event. An aggregate command commits its
final Plan and result in the same authoritative Plan event; Task, Stage,
Instance, workspace-summary and idempotency files are projections rebuilt from
that record. A crash after event append but before any projection update is
completed by replay. A crash after snapshot rename but before index update is
repaired by index rebuild. A snapshot revision newer than its event stream is
corruption and never becomes visible as a committed object.

NDJSON records carry a SHA-256 checksum of their canonical JSON body. Recovery
truncates only an incomplete or invalid tail to the last verified newline and
emits a persistent recovery warning. Corruption before the tail is fatal because
silently skipping an interior audit event would fork history.

Idempotency keys are scoped to a command target. The stored request digest
includes the command name and public payload. Reusing a key with the same digest
returns the original result; a different digest returns
`IDEMPOTENCY_CONFLICT`. The standalone idempotency file is not a second commit:
if it is missing after a crash, startup or lazy replay recreates it from the
authoritative event without executing the command again.

The saved Plan is authoritative for its active Stage and Instance sets. Plan
replacement retires projections absent from the new topology after the Plan
event commits. Recovery first replays the event and then performs the same
retirement, so a crash cannot leave an obsolete Stage or Instance visible.

Indexes are projections. `task-index.json` is rebuilt from committed task
snapshots; `inbox-index.json` is rebuilt from committed inbox snapshots. Neither
can create or advance an object.
