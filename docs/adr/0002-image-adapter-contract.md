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

Approval resolution is capability-driven. `approve`, `answer`, `select_master`,
`approve_skill`, `approve_additional_rounds`, `accept`, `regenerate` and
`terminate` may be sent only when present in the frozen capability list. The
Adapter maps the resolution to the Image `AdvanceRequest` field for that
capability and submits it through the asynchronous job endpoint with the
Harness idempotency key. Unknown phases or capabilities fail closed with
`VALIDATION_ERROR` and are recorded for adapter compatibility review.

### Delivery boundary

An Image `artifact://` reference is process-local. Completion discovery resolves
it only inside the instance output root and creates a candidate publication
request. Asset Service validates ownership and content, copies and re-hashes the
final public file, commits its manifest and `ASSET_PUBLISHED`, and only then may
the Adapter report the required delivery as complete. No local path crosses the
Harness contract boundary.

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
