# ADR 0002: Image Adapter mapping and external boundaries

- Status: Accepted
- Date: 2026-08-20
- Harness contract: `1.1`
- Image Agent commit: `61c5b4f1b66d5d85f62b39b5b338ac2304e94d26`

## Decision

The Image Adapter implements an explicit, versioned translation. It never
passes a Harness TaskCard through as an ImageTaskCard and never infers a value
that the mapping table marks as required.

### Task card mapping

| Harness TaskCard | ImageTaskCard | Rule |
| --- | --- | --- |
| `card_id` | no field | Adapter audit metadata only. |
| `task_id` | `parent_task_id` | Preserve the main-task identity. |
| `instance_id` | `task_id` | One Image project per Harness instance. |
| `instance_id` | `project_id` | The process-private project identifier is identical to the instance id. |
| `objective` | `deliverable_goal` | Exact UTF-8 string; required and non-empty. |
| `instructions` | `known_facts.harness_instructions` | Preserve order as a string array; never concatenate silently. |
| `parameters.usage_context` | `usage_context` | Required Adapter extension. Absence is `VALIDATION_ERROR`; no default. |
| `input_assets[*].asset_id` | `asset_inputs[*].asset_id` | Exact id after manifest validation. |
| input asset manifest `kind` | `asset_inputs[*].asset_type` | Derived only from the verified manifest. |
| input asset description + card instruction | `asset_inputs[*].usage_rule` | Explicit non-empty rule; absence is `VALIDATION_ERROR`. |
| verified publication/import fact | `asset_inputs[*].verified` | `true` only after Harness manifest verification. |
| input asset manifest | `source_refs[*]` | `ref_id=asset_id`, `ref_type=kind`, description excerpt, final-file SHA-256. |
| `parameters.category_id/version` | `category_ref` | Both fields mapped directly when present. |
| remaining public `parameters` | `known_facts.harness_parameters` | Only schema-declared public fields; credentials and paths are forbidden. |
| `expected_deliveries` + `parameters.aspect_ratio` | `known_facts.harness_output_contract` | Preserve the required role/kind/MIME set and aspect ratio before any paid call. |
| no Harness equivalent | Image workflow continuation fields | Omitted on creation; owned by Image Agent after creation. |
| fixed literal | `status` | `draft` on first creation only. |

Contract `1.1` declares `usage_context` and the optional category pair needed by
the lossless mapping. The G2 Adapter validates the Harness card first, verifies
every referenced asset against its authoritative manifest, then validates the
mapped payload against the consumer's pinned `ImageTaskCard.schema.json`. It
returns `SCHEMA_VERSION_UNSUPPORTED` or `VALIDATION_ERROR` on incompatibility;
it never hides a missing field with a default.

### Status and approval mapping

| Image observation | Harness instance state / action |
| --- | --- |
| process booting, health not ready | `STARTING` |
| healthy and workflow can advance without input | `RUNNING` |
| `snapshot.waiting=true` with non-empty `capabilities` | `WAITING_APPROVAL`; freeze `phase` as `step_id` and capabilities in request payload |
| terminal delivery verified by Image Agent, not yet published | remain `RUNNING` |
| all required deliveries published by Asset Service | `SUCCEEDED` |
| persisted workflow failure | `FAILED` |
| process exits unexpectedly | `CRASHED` |

Approval resolution is capability-driven. Only capabilities with a strict Image
`AdvanceRequest` mapping are exposed to Harness approval: taskbook/final approval,
clarification answers and safe defaults, category/skill decisions, taskbook revision,
master selection, calibration review, human prompt tuning and deterministic no-payload
continuations. Workbench-only capabilities are not presented as control-plane actions.
The Adapter submits the mapped payload through the asynchronous job endpoint with a
derived Harness idempotency key and reconciles that job on later observations. Unknown
phases/capabilities and unsupported payload fields fail closed.

### Delivery boundary

An Image `artifact://` reference is process-local. Completion discovery accepts only the
Agent's finalized delivery marker and envelope, opens the delivery through descriptor-safe
non-following reads, verifies its SHA-256 and stages it into the instance output boundary.
When the pinned Provider returns JPEG for an explicit PNG-only contract, the Adapter creates
a deterministic RGB PNG in the private output boundary and records source/derived hashes and
conversion parameters. Asset Service validates the complete required set before publication,
prepares every manifest under one visibility batch, and exposes that batch only after the
instance is durably `SUCCEEDED`. A rejected set stays private and is projected as `FAILED`
with structured `delivery_rejection`; the dedicated retry command reuses the finalized Agent
output and does not replay paid model steps. No Agent-local path crosses the Harness contract
boundary.

### Token boundary

Provider usage is accepted only from a backward-compatible Image observation
hook/API that returns the provider request id, model and raw token counters. The
Adapter maps those facts to `TokenUsageEvent`. Missing usage is `unreported`,
never numeric zero or an estimate. Until the hook exists, automatic retry that
requires a token upper bound is denied and routed to human approval.

### Credentials and Provider mapping

Every credential pair declares exactly one Provider and a complete environment
mapping for that Provider. Phase 1 acceptance uses the offline fake Provider;
the optional real smoke uses one explicitly allow-listed Provider. A pair is
never sprayed into `ARK_*`, `OPENAI_*` and `VLM_*` simultaneously, and Key and
Base URL are always resolved from the same pinned pair revision.
