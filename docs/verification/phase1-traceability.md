# Phase 1 acceptance traceability

This matrix maps RFC v0.2 section 19 to the work package and executable evidence.
G5 closes the product UI, public API, adversarial/recovery suite and reproducible
release-evidence package. The PPT Adapter remains honestly unavailable; no row
claims a real PPT delivery.

| RFC 19 | Work package | Evidence | Current status |
| --- | --- | --- | --- |
| 1 | P1-03/P1-04 | task/input commands, controlled asset import and task-panel browser tests | G5 complete |
| 2 | P1-03/P1-07/P1-08 | plan tests plus G2 and G4 real offline Image launches | G5 complete |
| 3 | P1-07/P1-14 | supervisor isolation tests plus `test_g4_multi_image_agent` | G5 complete |
| 4 | P1-07/P1-12 | instance API and Playwright task→instance→workbench/deep-link flow | G5 complete |
| 5 | P1-05/P1-13 | credential adversarial tests, redacted UI and real `1→2→3→1` allocation | G5 complete |
| 6 | P1-06 | configuration tests, revision UI and real local→global overwrite | G5 complete |
| 7 | P1-02/P1-04 | layout plus traversal/symlink/browser-boundary tests | complete |
| 8 | P1-04 | publication visibility, forged-file/manifest, crash and corruption tests | complete |
| 9 | P1-11/P1-12 | Asset Service safe list/preview/download tests; API and resource-browser E2E | complete |
| 10 | P1-09 | owner freeze, monotonic sequence, revision/idempotency and restart-dedupe tests | complete |
| 11 | P1-08/P1-10/P1-12 | `test_usage_budget`, G4 API and Playwright usage UI | G5 complete |
| 12 | P1-09 | unread/handled reducer, notification dedupe and approval deep-link browser tests | complete |
| 13 | P1-02/P1-07/P1-13 | crash-window tests plus G4 control-plane restart with stable real job IDs | G5 complete |
| 14 | P1-03/P1-08 | topology recovery, typed unavailable PPT Adapter and real Image E2E | G5 complete |
| 15 | P1-03 | delayed required-PPT blocking tests | complete |
| 16 | P1-03/P1-09 | aggregation priority tests | complete |
| 17 | P1-07/P1-08 | process-group tests plus real Adapter job cancellation with surviving peers | G5 complete |
| 18 | P1-10/P1-13 | concurrent/replay/replacement/unknown-cost budget adversarial tests | G5 complete |

## P1-00 fixtures

- Test port range: `18100-18199`; allocation tests bind before claiming a port.
- Temporary root: test-framework-owned directories only; no repository runtime
  state is reused between tests.
- Credential pairs: `tests/fixtures/p1/credential-pairs.json`. Values are
  non-routable, test-only markers and are never accepted by a real smoke.
- Fake Image mode: no network, deterministic jobs, explicit token usage and
  deterministic candidate files.
- Real Provider smoke: disabled by default and CI; requires
  `HARNESS_REAL_PROVIDER_SMOKE=1`, an allow-listed Provider and environment-only
  credentials. It may create one bounded request and must redact all output.

## G2 gate evidence

- Consumer mapping: `tests/unit/test_image_adapter.py` validates TaskCard 1.1,
  authoritative assets, lossless mapped fields and fail-closed phase/capability handling.
- Versioned API: `tests/integration/test_app.py` exercises task creation, plan persistence,
  task/instance reads and typed Adapter discovery through the application boundary.
- Real Agent: `make g2-e2e` launches Image Agent
  `main@2339550ab15ad05e0dde7f48e1386a5a1a0eb663` offline, observes its deferred Job,
  Timeline and Snapshot until `WAITING_APPROVAL`, then opens its HTTP workbench.
- Browser: `frontend/e2e/shell.spec.ts` covers task and instance navigation, workbench deep
  link, small-screen overflow, keyboard-visible controls and responsive layout.
- Production browser: `make frontend-integration` builds the release bundle and drives all
  three approvals through the UI against real Harness/Image processes plus a deterministic
  local Provider, without browser request mocks. `make real-provider-smoke` exposes the same
  isolated path only when dedicated external Provider credentials are supplied explicitly.

## G3 gate evidence

- Approval/Inbox: `tests/integration/test_approvals.py` covers frozen routing, global monotonic
  sequence, FIFO order, separate read/handled transitions, optimistic revision checks,
  command idempotency and restart-safe deduplication.
- Application recovery: `tests/integration/test_application_service.py` injects a crash after
  Adapter advance acceptance and proves recovery does not advance twice. Adapter rejection keeps
  the approval pending, and the delivery gate rejects wrong actors and pending approvals before
  publication; completed observations must publish every required asset before the instance or
  task can become `SUCCEEDED` and emit terminal notifications.
- Image boundary: `tests/unit/test_image_adapter.py` verifies capability filtering, strict
  `AdvanceRequest` payload mapping, finalized-envelope digest checks and descriptor-safe staging.
- API/browser: `tests/integration/test_app.py` covers approval, inbox, routing and safe resource
  endpoints. `frontend/e2e/shell.spec.ts` covers actionable approval UX, handled state,
  resource preview/download discovery, keyboard reachability and mobile overflow.
- Exit gate: `make g3-e2e` runs the scripted fault-isolation scenario and
  `tests/e2e/test_g3_real_image_agent.py`. The latter launches the pinned Image Adapter and real
  Image process against a deterministic local HTTP provider, crosses every human approval,
  finalizes the Image delivery, publishes the verified asset and completes the instance/task.

## G4 gate evidence

- Usage: `tests/integration/test_usage_budget.py` validates ownership, credential linkage,
  idempotency conflict, completeness, model/time/instance aggregation, integer micro-cost and
  reconstruction. Image explicitly returns no events when its pinned API has no usage source.
- Retry budget: the same suite covers task-lock check+reservation, concurrent hard limits,
  replay, replacement-instance lineage, unknown costs, human-only one-shot overrides,
  settlement, over-reservation freeze and approval recovery.
- Adapter/API: `tests/unit/test_image_adapter.py` covers policy/model hot apply and active-job
  cancellation. `tests/integration/test_g4_api.py` covers usage, retry-budget, global/instance
  config and secret-redacted key-pool boundaries.
- Browser: `frontend/e2e/shell.spec.ts` covers Token/cost/budget visibility, explicit
  completeness, redacted credentials, cleared secret input, keyboard access and mobile overflow.
- Exit gate: `make g4-e2e` launches three pinned real Image processes, proves isolated
  PIDs/ports/directories, completes three approval-to-verified-delivery workflows and reaches task
  `SUCCEEDED`. A separate regression freezes monitoring, sends `SIGKILL` to one process group,
  restarts the control plane, cancels a peer and proves the third job survives without replay.

## G5 gate evidence

- Public API: stable opaque keyset pagination, controlled asset import, task-scoped approvals,
  redacted audit events and task/instance lifecycle routes run blocking work off the event loop.
- Product UI: Playwright covers task overview, instance, resources, approvals, Token and event
  views, including keyboard navigation, 375px/landscape overflow, async disabling and error recovery.
- Release quality: `make verify` adds full-project Pyright and Python/npm vulnerability audits;
  startup fails closed when the selected Linux/Windows native process primitives are unavailable.
- Operations: `docs/operations.md` specifies installation, backup, restore, log redaction,
  upgrade/rollback and single-machine limits. `docs/master-api-guide.md` freezes the Master-facing
  command, revision, pagination, error and secret-handling conventions.
- Evidence: `make g5-e2e` records all four gate exit codes and log digests before generating the
  RFC 1–18 file-hash index. `make evidence` alone rejects missing, failed, dirty or cross-commit
  gate results instead of treating source-file existence as acceptance.
