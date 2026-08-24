"""Typed professional-Agent boundary records.

The frozen JSON Schemas remain the runtime authority. These shapes make the
Application/Adapter hand-off explicit so a new PPT adapter does not introduce
more free-form orchestration dictionaries.
"""

from __future__ import annotations

from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

AgentType = Literal["image", "ppt", "master"]
AgentInstanceStatus = Literal[
    "CREATED",
    "READY",
    "UNAVAILABLE",
    "STARTING",
    "RUNNING",
    "WAITING_APPROVAL",
    "FAILED_TO_START",
    "SUCCEEDED",
    "FAILED",
    "CRASHED",
    "CANCELLED",
    "SUPERSEDED",
    "ARCHIVED",
]
DeliveryKind = Literal["image", "presentation", "document", "archive", "other"]


class ProcessProjection(TypedDict):
    pid: int
    port: int
    launch_id: str
    state: Literal["STARTING", "RUNNING", "EXITED"]
    started_at: str


class DeliveryRejection(TypedDict):
    code: str
    message: str
    details: dict[str, Any]
    rejected_at: str
    retryable: bool


class StartFailure(TypedDict):
    code: str
    message: str
    details: dict[str, Any]
    phase: str
    operation_id: str
    attempt: int
    retryable: bool
    failed_at: str


class AuthorizedDowngrade(TypedDict):
    authorization_id: str
    authorized_at: str
    authorized_by_type: Literal["human", "master"]
    authorized_by_id: str
    plan_revision: int
    reason: str


class RequirementLifecycle(TypedDict):
    original_required: bool
    first_activated_at: str | None
    authorized_downgrade: AuthorizedDowngrade | None


class AgentInstanceSnapshot(TypedDict):
    schema_version: str
    instance_id: str
    task_id: str
    stage_id: str
    agent_type: Literal["image", "ppt"]
    required: bool
    requirement_lifecycle: RequirementLifecycle
    status: AgentInstanceStatus
    approval_mode: Literal["human", "master"]
    config_revision: int
    workspace_relpath: str
    task_card_relpath: str
    ui_url: str | None
    process: ProcessProjection | None
    created_at: str
    restart_required: NotRequired[bool]
    delivery_rejection: NotRequired[DeliveryRejection | None]
    start_failure: NotRequired[StartFailure | None]


class StageSnapshot(TypedDict):
    schema_version: str
    stage_id: str
    task_id: str
    type: Literal["image", "ppt"]
    position: int
    depends_on: list[str]
    required: bool
    requirement_lifecycle: RequirementLifecycle
    status: str
    instance_ids: list[str]


class AssetReference(TypedDict):
    asset_id: str
    manifest_relpath: str


class ExpectedDelivery(TypedDict):
    kind: DeliveryKind
    role: str
    required: bool
    accepted_mime_types: list[str]


class TaskCard(TypedDict):
    schema_version: str
    card_id: str
    revision: int
    task_id: str
    stage_id: str
    instance_id: str
    agent_type: Literal["image", "ppt"]
    objective: str
    instructions: list[str]
    input_assets: list[AssetReference]
    expected_deliveries: list[ExpectedDelivery]
    parameters: dict[str, Any]
    created_at: str
    retry_group_id: NotRequired[str]


class DeliveryCandidate(TypedDict):
    source_relative_path: str
    kind: DeliveryKind
    role: str
    description: str
    mime_type: str
    size_bytes: int
    sha256: str
    derivation: dict[str, Any] | None


class DeliveryBundleFile(TypedDict):
    private_relative_path: str
    mime_type: str
    size_bytes: int
    sha256: str


class DeliveryBundleImage(DeliveryBundleFile):
    width: int
    height: int


class DeliveryBundleCandidate(TypedDict):
    schema_version: str
    bundle_id: str
    task_id: str
    work_item_id: str
    instance_id: str
    task_card_revision: int
    branch_id: str
    checkpoint_id: str
    image: DeliveryBundleImage
    design_note: DeliveryBundleFile
    status: Literal["PENDING_CONFIRMATION", "PUBLISHED", "REJECTED", "CORRUPTED"]
    created_at: str
    decided_at: str | None
    actor: dict[str, str] | None
    publication_batch_id: str | None


class BillingUnit(TypedDict):
    unit: str
    quantity: int
    attributes: dict[str, Any]


class UsageEvent(TypedDict):
    schema_version: str
    event_id: str
    task_id: str
    instance_id: str
    agent_type: AgentType
    request_id: str
    provider_request_id: str | None
    provider: str
    model: str
    call_type: str
    usage_basis: Literal["tokens", "image_units", "mixed"]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    total_tokens: int
    billing_units: list[BillingUnit]
    raw_usage: dict[str, Any]
    occurred_at: str
    cost_micros: NotRequired[int]
    price_catalog_revision: NotRequired[str]
