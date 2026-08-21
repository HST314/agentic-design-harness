# Phase 1 acceptance traceability

This matrix maps RFC v0.2 section 19 to the work package and executable evidence
that must close it. `G4 complete` means usage, retry budgets, Image runtime control
and the real three-process gate are closed; it does not claim the final G5 product,
adversarial and release evidence packages.

| RFC 19 | Work package | Evidence | Current status |
| --- | --- | --- | --- |
| 1 | P1-03/P1-04 | task/input command tests; `test_assets` import/selection tests | service complete; UI pending |
| 2 | P1-03/P1-07/P1-08 | plan tests plus G2 and G4 real offline Image launches | G4 multi-instance complete |
| 3 | P1-07/P1-14 | supervisor isolation tests plus `test_g4_multi_image_agent` | G4 real three-process gate complete |
| 4 | P1-07/P1-12 | instance API, G2 real-Agent gate and Playwright task→instance→workbench flow | G3 single-instance complete |
| 5 | P1-05/P1-13 | credential service tests plus G4 real `1→2→3→1` allocation | G4 gate complete; final security audit pending |
| 6 | P1-06 | configuration service tests plus G4 real local→global hot-apply overwrite | G4 gate complete |
| 7 | P1-02/P1-04 | layout plus traversal/symlink/browser-boundary tests | complete |
| 8 | P1-04 | publication visibility, forged-file/manifest, crash and corruption tests | complete |
| 9 | P1-11/P1-12 | Asset Service safe list/preview/download tests; API and resource-browser E2E | complete |
| 10 | P1-09 | owner freeze, monotonic sequence, revision/idempotency and restart-dedupe tests | complete |
| 11 | P1-08/P1-10/P1-12 | `test_usage_budget`, G4 API and Playwright usage UI | G4 gate complete |
| 12 | P1-09 | unread/handled reducer, notification dedupe and approval deep-link browser tests | complete |
| 13 | P1-02/P1-07/P1-13 | launch/attempt tests plus G4 control-plane restart with stable real job IDs | G4 recovery gate complete; crash-kill final E2E pending |
| 14 | P1-03/P1-08 | topology replacement/recovery tests, typed unavailable Adapter and real Image E2E | G2 single-instance complete |
| 15 | P1-03 | delayed required-PPT blocking tests | complete |
| 16 | P1-03/P1-09 | aggregation priority tests | complete |
| 17 | P1-07/P1-08 | process-group tests plus real Adapter job cancellation with surviving peers | G4 gate complete |
| 18 | P1-10/P1-13 | concurrent/replay/replacement/unknown-cost budget adversarial tests | G4 gate complete |

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
  `main@61c5b4f1b66d5d85f62b39b5b338ac2304e94d26` offline, observes its deferred Job,
  Timeline and Snapshot until `WAITING_APPROVAL`, then opens its HTTP workbench.
- Browser: `frontend/e2e/shell.spec.ts` covers task and instance navigation, workbench deep
  link, small-screen overflow, keyboard-visible controls and responsive layout.

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
- Exit gate: `make g4-e2e` launches three pinned real Image processes offline, proves isolated
  PIDs/ports/directories, `1→2→3→1` credential allocation, local-to-global config overwrite,
  explicit unreported usage, restart recovery with unchanged job IDs, and independent cancel.
