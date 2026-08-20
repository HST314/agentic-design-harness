# ADR 0007: Typed Adapter registry and application use-case boundary

- Status: Accepted
- Date: 2026-08-20

## Decision

Professional Agents implement one runtime-checkable `AgentAdapter` Protocol. Its explicit
values cover card validation, runtime preparation, asynchronous start/advance/configuration,
status, deliveries, usage, UI deep links and recovery. `AdapterRegistry` owns the mapping from
`agent_type` to one adapter; callers never branch on Image/PPT inside API handlers.

Phase 1 registers `PptAgentContractAdapter` as an intentionally unavailable contract
placeholder. It validates PPT cards and reports `UNAVAILABLE`, while every operational call
fails with the stable `ADAPTER_UNAVAILABLE` code. G2 also registers the runnable Image adapter;
the application layer still dispatches only through the typed registry and does not simulate
or branch on Image behavior inside the control-plane core.

`HarnessApplicationService` is the only public boundary for workflows spanning domain,
credential, asset, process and Adapter services. Plan save plus instance creation records a
durable intent before side effects, validates before allocation, serializes operations per
task, derives stable child idempotency identities and can finish after a crash. It also owns
confirm/start, cancel/archive and publish/complete ordering so API and Master consumers cannot
recreate unsafe call sequences.
