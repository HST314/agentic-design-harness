# ADR 0004: Phase 1 assets, credentials, configuration and process supervision

- Status: Accepted
- Date: 2026-08-20
- Scope: P1-04 through P1-07

## Decision

P1 runtime services remain internal application services until the versioned
HTTP boundary is added by P1-11. They are composed and recovered by the FastAPI
application lifecycle, but callers cannot bypass the domain state machine by
starting a process or writing a projection directly.

### Assets

Every task receives input, selected-input, shared-resource, manifest, approval
and instance-private directories. All service paths are normalized task-relative
POSIX paths; absolute paths, traversal, Windows paths, symlinks and paths outside
the allowed resource roots fail closed.

Imports and publications stream through bounded temporary files. A publication
copies from one instance's private `outputs` directory, flushes and atomically
renames the final public file, then re-reads its MIME, size and SHA-256. The
`ASSET_PUBLISHED` event is the visibility commit. A final file or manifest
without that event is not a registered delivery. Recovery resumes prepared
publications, recreates projections after the commit and marks digest mismatches
`CORRUPTED`, which prevents downstream selection.

### Credentials

Key and Base URL form one immutable, Provider-specific credential-pair revision.
The secret history is stored only under `control-data/secrets` with mode `0600`;
events, instance projections and public summaries contain only pair identity and
redacted metadata. An HMAC over the complete pair, including the API Key, binds
the secret revision to its committed assignment and detects out-of-band edits.

`CREDENTIAL_PAIR_ASSIGNED` is the instance-creation commit. Allocation is
serialized by the pool lock and the Provider cursor is derived from committed
creation events, so a pre-commit crash does not consume a position and a
post-commit crash cannot allocate twice. Reassignment replaces the complete
pair and does not advance the natural cursor. A process wrapper reads a `0600`
launch file, removes it immediately, injects the pinned pair only into the child
environment and redacts both exact values across log-buffer boundaries.

### Configuration

Global and instance configuration use closed Pydantic schemas. Agent code and
credential fields are not mutable configuration. New instances receive a full
global snapshot; local updates create a new instance revision; a global commit
locks all affected tasks in stable order and replaces every non-archived
instance snapshot. `GLOBAL_CONFIG_COMMITTED` is authoritative, and startup
replays any incomplete YAML or instance projection.

Configuration already attached to an in-flight model-call attempt is never
rewritten. A running process either hot-applies a validated snapshot through the
future Adapter callback or persists `restart_required=true`. A controlled
restart clears that flag only when the exact launched config revision becomes
ready, so a newer concurrent update is not accidentally acknowledged.

### Process supervision

Each instance has one private working directory, one persisted port claim and
one process group led by a persistent redacting wrapper. A launch records its
request digest, attempt, fixed code identity, PID start identity, child identity,
port and configuration revision. Reusing a launch id for any different request
is an idempotency conflict; starting an unactivated or unconfirmed plan is
rejected.

Startup requires both health and readiness before business state becomes
`RUNNING`. Startup timeout becomes `FAILED_TO_START`; later unexpected exit
becomes `CRASHED`. Monitoring uses PID start identity rather than PID alone,
interrupts in-flight model-call attempts without replay, and never follows HTTP
redirects. Cancel and archive stop the whole process group with TERM then KILL,
release the port, retain the workspace and make an archived workspace read-only.
Startup reconciliation adopts the same live wrapper and promotes a ready
`STARTING` launch instead of creating a second process.

## Consequences

- The runtime is intentionally single-machine and same-user; filesystem and
  service boundaries are not described as container isolation.
- Automatic child restart and model-call replay remain forbidden. Recovery
  reports durable state so a later explicit command can decide what to retry.
- HTTP routes, Adapter integration, UI and the broader adversarial acceptance
  suite remain owned by P1-08 and P1-11 through P1-14.

## Executable evidence

- `tests/integration/test_assets.py`
- `tests/integration/test_credentials.py`
- `tests/integration/test_configuration.py`
- `tests/integration/test_supervisor.py`
- `tests/fixtures/fake_agent_process.py`
