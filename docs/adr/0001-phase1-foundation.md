# ADR 0001: Phase 1 foundation and process layout

- Status: Accepted
- Date: 2026-08-20
- Baseline: `main@169716b8381f52a0e71b4cf5911c34d3e994cc4d`
- RFC: `docs/rfc-v0.2.md`, SHA-256 `b44e9c6877ccc3b282ddf9afcbeb1e15a763e0c4cfcc397df13c1ca333a808da`
- Contract catalog: `contracts/v1`, schema version `1.0`
- Image Agent: `main@2339550ab15ad05e0dde7f48e1386a5a1a0eb663`
- Image Agent lock SHA-256: `97123ccc4cd263dff84eb9383979dbd462a03cb7899f75b5386921e08b49df5c`

## Decision

The Harness backend uses Python 3.10+, FastAPI, Pydantic v2, Uvicorn and
PyYAML. The web shell uses TypeScript and Vite without a framework dependency;
the Phase 1 product pages can add view components behind the same typed API
client and routes. Python and npm dependencies are pinned by generated lock
files and verified by CI.

The Harness, every Image Agent instance and the future PPT Agent run in
separate operating-system processes and separate dependency environments. The
Harness communicates through HTTP and persisted contracts. Runtime code is
forbidden from importing an Agent repository or internal package.

The control plane is the only state writer. It owns `control-data/`; each main
task owns `workspace/tasks/<task_id>/`. Agent processes never write control
plane snapshots and only write their assigned instance work directories.

The backend starts with a process-wide writer lease. Task mutations additionally
use short task locks. Credential allocation and other truly global counters use
short global locks. A lock timeout is a stable conflict, never permission to
continue unlocked.

## Runtime and error boundary

- API payloads are validated against `contracts/v1` before domain logic.
- Domain commands carry an idempotency key, actor and expected revision.
- Stable errors come from `contracts/v1/catalogs/error-codes.json`; internal
  exceptions and secrets are not reflected to clients.
- JSON logs contain timestamp, level, logger, event and trace identifiers. The
  logger redacts credential-shaped keys and values.
- Readiness requires a valid contract registry and an acquired writer lease;
  liveness only proves that the process event loop responds.

## Frontend boundary

The Harness owns task-level navigation, status and control surfaces. A
professional Agent workflow is opened through an adapter-provided HTTP deep
link. The web shell must not derive local paths or construct an Agent URL from
PID/port fields.

## Test layout

- `tests/unit`: pure domain, configuration and storage primitives.
- `tests/contract`: runtime validation of the frozen contract source.
- `tests/integration`: application and repository boundaries.
- `tests/crash`: commit-point interruption and recovery.
- `frontend/e2e`: browser-level shell and routing checks.

Real Provider calls are excluded from default and CI test commands. They require
an explicit smoke flag, an allow-listed Provider and credentials supplied only
through the process environment.
