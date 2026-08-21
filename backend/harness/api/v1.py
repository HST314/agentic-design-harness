"""Versioned HTTP use cases for the Phase 1 product control plane."""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import read_json
from ..storage.layout import validate_identifier
from ..storage.ndjson import recover_records
from ..storage.repository import Actor
from .pagination import paginate

if TYPE_CHECKING:
    from .app import Container


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(StrictRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "task_id": "task_campaign_01",
                    "title": "Campaign visual",
                    "goal": "Create three reviewable visual directions.",
                    "master_owner": "master_default",
                    "start_policy": "manual",
                    "input_manifest": "inputs/manifests/input_rev_1.json",
                    "envelope": {
                        "idempotency_key": "create_task_campaign_01",
                        "actor_type": "master",
                        "actor_id": "master_default",
                        "expected_revision": 0,
                    },
                }
            ]
        },
    )
    task_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1, max_length=20_000)
    master_owner: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    start_policy: Literal["manual", "auto"] = "manual"
    input_manifest: str = Field(min_length=1, max_length=512)
    envelope: CommandEnvelope


class SavePlanRequest(StrictRequest):
    stages: list[dict[str, Any]]
    instances: list[dict[str, Any]]
    task_cards: list[dict[str, Any]]
    providers: dict[str, str]
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class StartTaskRequest(StrictRequest):
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class InstanceOperationRequest(StrictRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "operation_id": "instance_operation_01",
                    "envelope": {
                        "idempotency_key": "instance_operation_01",
                        "actor_type": "human",
                        "actor_id": "operator",
                        "expected_revision": 3,
                    },
                }
            ]
        },
    )
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class ImportAssetRequest(StrictRequest):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=30_000_000)
    description: str = Field(default="", max_length=4000)
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class ResolveApprovalRequest(StrictRequest):
    decision: Literal["APPROVED", "REJECTED"]
    action: str | None = Field(default=None, min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class InboxStatusRequest(StrictRequest):
    status: Literal["READ", "HANDLED"]
    envelope: CommandEnvelope


class ApprovalModeRequest(StrictRequest):
    approval_mode: Literal["human", "master"]
    envelope: CommandEnvelope


class PublishDeliveryRequest(StrictRequest):
    source_relative_path: str = Field(min_length=1, max_length=1024)
    role: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class ConfigWriteRequest(StrictRequest):
    config: dict[str, Any]
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class InstanceConfigWriteRequest(StrictRequest):
    patch: dict[str, Any]
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class CredentialPoolWriteRequest(StrictRequest):
    pairs: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    envelope: CommandEnvelope


class ReassignCredentialRequest(StrictRequest):
    credential_pair_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    credential_pair_revision: int = Field(ge=1)
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class UsageBatchRequest(StrictRequest):
    events: list[dict[str, Any]] = Field(max_length=10_000)
    cursor: str | None = Field(default=None, max_length=256)
    collection_complete: bool | None = None
    envelope: CommandEnvelope


class RetryBudgetWriteRequest(StrictRequest):
    retry_policy: dict[str, Any]
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class RetryRequest(StrictRequest):
    attempt_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    retry_of_attempt_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    reservation_tokens: int | None = Field(default=None, ge=1)
    estimated_cost_micros: int | None = Field(default=None, ge=0)
    price_catalog_revision: str | None = Field(
        default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"
    )
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class ConsumeRetryOverrideRequest(StrictRequest):
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


class SettleRetryRequest(StrictRequest):
    actual_tokens: int = Field(ge=0)
    actual_cost_micros: int | None = Field(default=None, ge=0)
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    envelope: CommandEnvelope


def build_v1_router(container: Container) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/tasks", tags=["tasks"])
    async def create_task(body: CreateTaskRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            container.commands.create_task,
            task_id=body.task_id,
            title=body.title,
            goal=body.goal,
            master_owner=body.master_owner,
            start_policy=body.start_policy,
            input_manifest=body.input_manifest,
            envelope=body.envelope,
        )

    @router.get("/tasks", tags=["tasks"])
    async def list_tasks(
        status: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
        order: Literal["asc", "desc"] = Query(default="desc"),
    ) -> dict[str, Any]:
        def read_index() -> dict[str, Any]:
            index = read_json(
                container.store.layout.control_root / "indexes" / "task-index.json"
            )
            items = [
                item
                for item in index["tasks"]
                if status is None or item["status"] == status
            ]
            page = paginate(
                items,
                scope=f"tasks:{status or '*'}",
                fields=("updated_at", "task_id"),
                limit=limit,
                cursor=cursor,
                order=order,
            )
            page["items"] = [_task_summary(container, item) for item in page["items"]]
            return {
                "schema_version": "1.0",
                **page,
            }

        return await run_in_threadpool(read_index)

    @router.get("/tasks/{task_id}", tags=["tasks"])
    async def get_task(task_id: str) -> dict[str, Any]:
        def read_task() -> dict[str, Any]:
            validate_identifier(task_id, "task_id")
            task = container.store.task.get(task_id, task_id)
            if task is None:
                raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
            notifications = container.store.inbox.list(task_id)
            notifications.sort(
                key=lambda item: (item["created_at"], item["sequence"], item["inbox_id"]),
                reverse=True,
            )
            return {
                "schema_version": "1.0",
                "task": task,
                "task_revision": container.store.task.revision(task_id, task_id),
                "plan": container.store.plan.get(task_id, task_id),
                "recent_notifications": notifications[:5],
            }

        return await run_in_threadpool(read_task)

    @router.get("/tasks/{task_id}/events", tags=["audit"])
    async def list_task_events(
        task_id: str,
        actor_type: Literal["human", "master", "system", "adapter"] | None = Query(
            default=None
        ),
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
        order: Literal["asc", "desc"] = Query(default="desc"),
    ) -> dict[str, Any]:
        def read_events() -> dict[str, Any]:
            validate_identifier(task_id, "task_id")
            if container.store.task.get(task_id, task_id) is None:
                raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
            path = container.store.layout.control_root / "tasks" / task_id / "events.ndjson"
            events = [
                _public_audit_event(item)
                for item in recover_records(path)
                if item.get("event_type") == "OBJECT_COMMITTED"
                and (
                    actor_type is None
                    or (item.get("actor") or {}).get("actor_type") == actor_type
                )
            ]
            return {
                "schema_version": "1.0",
                "task_id": task_id,
                **paginate(
                    events,
                    scope=f"task-events:{task_id}:{actor_type or '*'}",
                    fields=("occurred_at", "event_id"),
                    limit=limit,
                    cursor=cursor,
                    order=order,
                ),
            }

        return await run_in_threadpool(read_events)

    @router.put("/tasks/{task_id}/plan", tags=["tasks"])
    async def save_plan(task_id: str, body: SavePlanRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            container.application.save_plan_and_create_instances,
            task_id,
            stages=body.stages,
            instances=body.instances,
            task_cards=body.task_cards,
            providers=body.providers,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )

    @router.post("/tasks/{task_id}/confirm-start", tags=["tasks"])
    async def confirm_start(task_id: str, body: StartTaskRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            container.application.confirm_and_start_ready_instances,
            task_id,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )

    @router.post("/tasks/{task_id}/cancel", tags=["tasks"])
    async def cancel_task(task_id: str, body: InstanceOperationRequest) -> dict[str, Any]:
        result = await run_in_threadpool(
            container.application.cancel_task,
            task_id,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", **result}

    @router.get("/instances/{instance_id}", tags=["instances"])
    async def get_instance(
        instance_id: str,
        refresh: bool = Query(default=True),
    ) -> dict[str, Any]:
        def read_instance() -> dict[str, Any]:
            task_id = _task_for_instance(container, instance_id)
            instance = container.store.instance.get(task_id, instance_id)
            if instance is None:
                raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
            if refresh and instance["status"] in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
                result = container.application.observe_instance(task_id, instance_id)
            else:
                result = {"instance": instance, "observation": None, "transition": None}
            pending = container.approvals.list_approvals(
                instance_id=instance_id, status="PENDING"
            )
            credential = next(
                (
                    item
                    for item in container.credentials.list_redacted()
                    if item["credential_pair_id"] == instance["credential_pair_ref"]
                    and item["revision"] == instance["credential_pair_revision"]
                ),
                None,
            )
            config = container.configuration.get_instance(task_id, instance_id)
            return {
                "schema_version": "1.0",
                "task_id": task_id,
                "task_revision": container.store.task.revision(task_id, task_id),
                "pending_approval": pending[0] if pending else None,
                "credential": credential,
                "config": config,
                **result,
            }

        return await run_in_threadpool(read_instance)

    @router.get("/instances/{instance_id}/approvals", tags=["approvals"])
    async def list_instance_approvals(
        instance_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
        order: Literal["asc", "desc"] = Query(default="desc"),
    ) -> dict[str, Any]:
        def read_approvals() -> dict[str, Any]:
            task_id = _task_for_instance(container, instance_id)
            return {
                "schema_version": "1.0",
                "task_id": task_id,
                **paginate(
                    container.approvals.list_approvals(instance_id=instance_id),
                    scope=f"instance-approvals:{instance_id}",
                    fields=("created_at", "sequence", "approval_id"),
                    limit=limit,
                    cursor=cursor,
                    order=order,
                ),
            }

        return await run_in_threadpool(read_approvals)

    @router.put("/instances/{instance_id}/approval-mode", tags=["approvals"])
    async def update_approval_mode(
        instance_id: str, body: ApprovalModeRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        return await run_in_threadpool(
            container.commands.set_approval_mode,
            task_id,
            instance_id,
            body.approval_mode,
            body.envelope,
        )

    @router.get("/instances/{instance_id}/ui-link", tags=["instances"])
    async def get_instance_ui_link(instance_id: str) -> dict[str, Any]:
        def read_link() -> dict[str, Any]:
            task_id = _task_for_instance(container, instance_id)
            instance = container.store.instance.get(task_id, instance_id)
            if instance is None:
                raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
            adapter = container.adapters.get(instance["agent_type"])
            return {
                "schema_version": "1.0",
                "instance_id": instance_id,
                "ui_url": adapter.get_ui_url(instance_id),
            }

        return await run_in_threadpool(read_link)

    @router.post("/instances/{instance_id}/cancel", tags=["instances"])
    async def cancel_instance(
        instance_id: str, body: InstanceOperationRequest
    ) -> dict[str, Any]:
        if body.envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human or Master may cancel an instance."
            )
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        instance = await run_in_threadpool(
            container.application.cancel_instance,
            task_id,
            instance_id,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", "instance": instance}

    @router.post("/instances/{instance_id}/start", tags=["instances"])
    async def start_instance(
        instance_id: str, body: InstanceOperationRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        result = await run_in_threadpool(
            container.application.start_instance,
            task_id,
            instance_id,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", **result}

    @router.post("/instances/{instance_id}/restart", tags=["instances"])
    async def restart_instance(
        instance_id: str, body: InstanceOperationRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        result = await run_in_threadpool(
            container.application.restart_instance,
            task_id,
            instance_id,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", **result}

    @router.post("/instances/{instance_id}/archive", tags=["instances"])
    async def archive_instance(
        instance_id: str, body: InstanceOperationRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        instance = await run_in_threadpool(
            container.application.archive_instance_command,
            task_id,
            instance_id,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", "instance": instance}

    @router.get("/adapters", tags=["instances"])
    async def list_adapters() -> dict[str, Any]:
        return {"schema_version": "1.0", "items": container.adapters.describe()}

    @router.get("/approvals/{approval_id}", tags=["approvals"])
    async def get_approval(approval_id: str) -> dict[str, Any]:
        result = await run_in_threadpool(container.approvals.get_approval, approval_id)
        return {"schema_version": "1.0", **result}

    @router.get("/tasks/{task_id}/approvals", tags=["approvals"])
    async def list_task_approvals(
        task_id: str,
        status: Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"] | None = Query(
            default=None
        ),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
        order: Literal["asc", "desc"] = Query(default="desc"),
    ) -> dict[str, Any]:
        items = await run_in_threadpool(
            container.approvals.list_approvals,
            task_id=task_id,
            status=status,
        )
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            **paginate(
                items,
                scope=f"task-approvals:{task_id}:{status or '*'}",
                fields=("created_at", "sequence", "approval_id"),
                limit=limit,
                cursor=cursor,
                order=order,
            ),
        }

    @router.post("/approvals/{approval_id}/resolve", tags=["approvals"])
    async def resolve_approval(
        approval_id: str, body: ResolveApprovalRequest
    ) -> dict[str, Any]:
        details = await run_in_threadpool(container.approvals.get_approval, approval_id)
        if details["approval"]["kind"] == "BUDGET_OVERRIDE":
            result = await run_in_threadpool(
                container.retry_budgets.resolve_approval,
                approval_id,
                decision=body.decision,
                action=body.action,
                payload=body.payload,
                envelope=body.envelope,
            )
        else:
            result = await run_in_threadpool(
                container.application.resolve_approval,
                approval_id,
                decision=body.decision,
                action=body.action,
                payload=body.payload,
                operation_id=body.operation_id,
                envelope=body.envelope,
            )
        return {"schema_version": "1.0", **result}

    @router.get("/inbox", tags=["inbox"])
    async def list_inbox(
        owner: Literal["human", "master"] = Query(default="human"),
        status: Literal["UNREAD", "READ", "HANDLED"] | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
        order: Literal["asc", "desc"] = Query(default="asc"),
    ) -> dict[str, Any]:
        items = await run_in_threadpool(
            container.approvals.list_inbox,
            owner=owner,
            status=status,
            limit=None,
        )
        return {
            "schema_version": "1.0",
            **paginate(
                items,
                scope=f"inbox:{owner}:{status or '*'}",
                fields=("created_at", "sequence", "inbox_id"),
                limit=limit,
                cursor=cursor,
                order=order,
            ),
        }

    @router.post("/inbox/{inbox_id}/status", tags=["inbox"])
    async def update_inbox_status(
        inbox_id: str, body: InboxStatusRequest
    ) -> dict[str, Any]:
        result = await run_in_threadpool(
            container.approvals.update_inbox_status,
            inbox_id,
            body.status,
            body.envelope,
        )
        return {"schema_version": "1.0", **result}

    @router.get("/tasks/{task_id}/files", tags=["assets"])
    async def list_task_files(
        task_id: str,
        group: Literal["inputs", "shared", "instances", "all"] = Query(default="all"),
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
        order: Literal["asc", "desc"] = Query(default="asc"),
    ) -> dict[str, Any]:
        files = await run_in_threadpool(container.assets.list_files, task_id, group)
        assets = await run_in_threadpool(container.assets.list_assets, task_id)
        page = paginate(
            files,
            scope=f"task-files:{task_id}:{group}",
            fields=("relative_path",),
            limit=limit,
            cursor=cursor,
            order=order,
        )
        return {"schema_version": "1.0", **page, "assets": assets}

    @router.post("/tasks/{task_id}/assets", tags=["assets"])
    async def import_task_asset(
        task_id: str, body: ImportAssetRequest
    ) -> dict[str, Any]:
        if body.envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human or Master may import a task asset."
            )
        def decode_and_import() -> dict[str, Any]:
            container.commands.validate_task_revision(
                task_id, body.envelope.expected_revision
            )
            try:
                content = base64.b64decode(body.content_base64, validate=True)
            except (ValueError, binascii.Error):
                raise HarnessError(
                    "ASSET_VALIDATION_FAILED", "The asset content is not valid Base64."
                ) from None
            return container.assets.import_bytes(
                task_id,
                filename=body.filename,
                content=content,
                description=body.description,
                source=f"api:{body.envelope.actor_type}:{body.envelope.actor_id}",
                idempotency_key=body.envelope.idempotency_key,
            )

        manifest = await run_in_threadpool(decode_and_import)
        return {"schema_version": "1.0", "manifest": manifest}

    @router.get("/tasks/{task_id}/files/preview", tags=["assets"])
    async def preview_task_file(task_id: str, path: str = Query(min_length=1)) -> Response:
        preview = await run_in_threadpool(container.assets.preview, task_id, path)
        content = preview["content"]
        return Response(
            content=content.encode("utf-8") if isinstance(content, str) else content,
            media_type=preview["mime_type"],
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @router.get("/tasks/{task_id}/files/download", tags=["assets"])
    async def download_task_file(
        task_id: str, path: str = Query(min_length=1)
    ) -> StreamingResponse:
        download = await run_in_threadpool(container.assets.download, task_id, path)
        headers = {
            **download["headers"],
            "Content-Length": str(download["size_bytes"]),
            "X-Content-SHA256": download["sha256"],
        }
        return StreamingResponse(
            download["stream"],
            media_type=download["mime_type"],
            headers=headers,
            background=BackgroundTask(download["stream"].close),
        )

    @router.post("/instances/{instance_id}/deliveries", tags=["assets"])
    async def publish_instance_delivery(
        instance_id: str, body: PublishDeliveryRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        result = await run_in_threadpool(
            container.application.publish_delivery_and_complete,
            task_id,
            instance_id,
            source_relative_path=body.source_relative_path,
            role=body.role,
            description=body.description,
            operation_id=body.operation_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", **result}

    @router.post("/instances/{instance_id}/deliveries/retry", tags=["assets"])
    async def retry_instance_delivery(
        instance_id: str, body: InstanceOperationRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        result = await run_in_threadpool(
            container.application.retry_rejected_delivery,
            task_id,
            instance_id,
            envelope=body.envelope,
        )
        return {"schema_version": "1.0", **result}

    @router.get("/config/global", tags=["configuration"])
    async def get_global_config() -> dict[str, Any]:
        config = await run_in_threadpool(container.configuration.get_global)
        return {"schema_version": "1.0", "config": config}

    @router.put("/config/global", tags=["configuration"])
    async def update_global_config(body: ConfigWriteRequest) -> dict[str, Any]:
        _require_human(body.envelope, "Only a human may revise global configuration.")
        config = await run_in_threadpool(
            container.configuration.save_global,
            body.config,
            expected_revision=body.envelope.expected_revision,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
        )
        return {"schema_version": "1.0", "config": config}

    @router.get("/instances/{instance_id}/config", tags=["configuration"])
    async def get_instance_config(instance_id: str) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        config = await run_in_threadpool(
            container.configuration.get_instance, task_id, instance_id
        )
        return {"schema_version": "1.0", "config": config}

    @router.put("/instances/{instance_id}/config", tags=["configuration"])
    async def update_instance_config(
        instance_id: str, body: InstanceConfigWriteRequest
    ) -> dict[str, Any]:
        if body.envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human or Master may revise instance configuration."
            )
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        config = await run_in_threadpool(
            container.configuration.update_instance,
            task_id,
            instance_id,
            body.patch,
            expected_revision=body.envelope.expected_revision,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
        )
        return {"schema_version": "1.0", "config": config}

    @router.get("/key-pool", tags=["configuration"])
    async def get_key_pool() -> dict[str, Any]:
        items = await run_in_threadpool(container.credentials.list_redacted)
        return {"schema_version": "1.0", "items": items}

    @router.put("/key-pool", tags=["configuration"])
    async def update_key_pool(body: CredentialPoolWriteRequest) -> dict[str, Any]:
        _require_human(body.envelope, "Only a human may revise the credential pool.")
        result = await run_in_threadpool(
            container.credentials.configure_pool, body.pairs
        )
        return {"schema_version": "1.0", **result}

    @router.post(
        "/instances/{instance_id}/reassign-credential-pair",
        tags=["configuration"],
    )
    async def reassign_credential_pair(
        instance_id: str, body: ReassignCredentialRequest
    ) -> dict[str, Any]:
        _require_human(body.envelope, "Only a human may reassign a credential pair.")
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        result = await run_in_threadpool(
            container.credentials.reassign_instance,
            task_id,
            instance_id,
            credential_pair_id=body.credential_pair_id,
            credential_pair_revision=body.credential_pair_revision,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
        )
        return {"schema_version": "1.0", "assignment": result}

    @router.get("/tasks/{task_id}/usage", tags=["usage"])
    async def get_task_usage(
        task_id: str, refresh: bool = Query(default=True)
    ) -> dict[str, Any]:
        if refresh:
            plan = await run_in_threadpool(container.store.plan.get, task_id, task_id)
            if plan is not None:
                for instance in plan["instances"]:
                    adapter = container.adapters.get_optional(instance["agent_type"])
                    if adapter is not None and adapter.available:
                        await run_in_threadpool(
                            container.usage.collect_instance,
                            task_id,
                            instance["instance_id"],
                            adapter,
                        )
        return await run_in_threadpool(container.usage.summary, task_id)

    @router.get("/instances/{instance_id}/usage", tags=["usage"])
    async def get_instance_usage(
        instance_id: str, refresh: bool = Query(default=True)
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        if refresh:
            instance = container.store.instance.get(task_id, instance_id)
            if instance is None:
                raise HarnessError(
                    "INSTANCE_NOT_FOUND", "The requested instance does not exist."
                )
            adapter = container.adapters.get(instance["agent_type"])
            if adapter.available:
                await run_in_threadpool(
                    container.usage.collect_instance, task_id, instance_id, adapter
                )
        return await run_in_threadpool(
            container.usage.summary, task_id, instance_id=instance_id
        )

    @router.post("/internal/instances/{instance_id}/usage-events", tags=["usage"])
    async def ingest_usage_events(
        instance_id: str, body: UsageBatchRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        instance = container.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError(
                "INSTANCE_NOT_FOUND", "The requested instance does not exist."
            )
        if (
            body.envelope.actor_type != "adapter"
            or body.envelope.actor_id != f"{instance['agent_type']}_adapter"
        ):
            raise HarnessError(
                "VALIDATION_ERROR", "Only the owning Agent adapter may report usage."
            )
        return await run_in_threadpool(
            container.usage.ingest,
            task_id,
            instance_id,
            body.events,
            source="internal",
            cursor=body.cursor,
            collection_complete=body.collection_complete,
        )

    @router.get("/tasks/{task_id}/retry-budget", tags=["usage"])
    async def get_retry_budget(task_id: str) -> dict[str, Any]:
        budget = await run_in_threadpool(container.retry_budgets.get, task_id)
        return {"schema_version": "1.0", "budget": budget}

    @router.put("/tasks/{task_id}/retry-budget", tags=["usage"])
    async def update_retry_budget(
        task_id: str, body: RetryBudgetWriteRequest
    ) -> dict[str, Any]:
        budget = await run_in_threadpool(
            container.retry_budgets.configure,
            task_id,
            body.retry_policy,
            expected_revision=body.envelope.expected_revision,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
        )
        return {"schema_version": "1.0", "budget": budget}

    @router.post("/instances/{instance_id}/retries", tags=["usage"])
    async def request_instance_retry(
        instance_id: str, body: RetryRequest
    ) -> dict[str, Any]:
        task_id = await run_in_threadpool(_task_for_instance, container, instance_id)
        return await run_in_threadpool(
            container.retry_budgets.request_retry,
            task_id,
            instance_id,
            attempt_id=body.attempt_id,
            retry_of_attempt_id=body.retry_of_attempt_id,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
            reservation_tokens=body.reservation_tokens,
            estimated_cost_micros=body.estimated_cost_micros,
            price_catalog_revision=body.price_catalog_revision,
        )

    @router.post("/retry-attempts/{attempt_id}/consume-override", tags=["usage"])
    async def consume_retry_override(
        attempt_id: str, body: ConsumeRetryOverrideRequest, task_id: str = Query()
    ) -> dict[str, Any]:
        if body.envelope.actor_type != "system":
            raise HarnessError(
                "VALIDATION_ERROR", "Only the retry executor may consume an override."
            )
        result = await run_in_threadpool(
            container.retry_budgets.consume_override,
            task_id,
            attempt_id,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
        )
        return {"schema_version": "1.0", "attempt": result}

    @router.post("/retry-attempts/{attempt_id}/settle", tags=["usage"])
    async def settle_retry(
        attempt_id: str, body: SettleRetryRequest, task_id: str = Query()
    ) -> dict[str, Any]:
        if body.envelope.actor_type not in {"adapter", "system"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only an Adapter or system may settle retry usage."
            )
        result = await run_in_threadpool(
            container.retry_budgets.settle,
            task_id,
            attempt_id,
            actual_tokens=body.actual_tokens,
            actual_cost_micros=body.actual_cost_micros,
            idempotency_key=body.operation_id,
            actor=Actor(body.envelope.actor_type, body.envelope.actor_id),
        )
        return {"schema_version": "1.0", **result}

    return router


def _task_summary(container: Container, index_item: dict[str, Any]) -> dict[str, Any]:
    task_id = index_item["task_id"]
    task = container.store.task.get(task_id, task_id)
    if task is None:
        raise HarnessError("INTERNAL_ERROR", "The task index references a missing task.")
    plan = container.store.plan.get(task_id, task_id)
    instances = [] if plan is None else plan["instances"]
    status_counts: dict[str, int] = {}
    for instance in instances:
        status_counts[instance["status"]] = status_counts.get(instance["status"], 0) + 1
    usage = container.usage.summary(task_id)
    notifications = container.store.inbox.list(task_id)
    notifications.sort(
        key=lambda item: (item["created_at"], item["sequence"], item["inbox_id"]),
        reverse=True,
    )
    return {
        **deepcopy(index_item),
        "goal": task["goal"],
        "start_policy": task["start_policy"],
        "stage_count": 0 if plan is None else len(plan["stages"]),
        "instance_count": len(instances),
        "instance_status_counts": status_counts,
        "total_tokens": usage["tokens"]["total_tokens"],
        "usage_completeness": usage["completeness"],
        "has_unavailable_ppt": any(
            stage["type"] == "ppt" and stage["status"] in {"UNAVAILABLE", "PENDING"}
            for stage in ([] if plan is None else plan["stages"])
        ),
        "latest_notification": deepcopy(notifications[0]) if notifications else None,
    }


def _public_audit_event(event: dict[str, Any]) -> dict[str, Any]:
    candidate_actor = event.get("actor")
    actor: dict[str, Any] = candidate_actor if isinstance(candidate_actor, dict) else {}
    return {
        "event_id": str(event["event_id"]),
        "event_type": "OBJECT_COMMITTED",
        "object_type": str(event["object_type"]),
        "object_id": str(event["object_id"]),
        "revision": int(event["revision"]),
        "actor": {
            "actor_type": str(actor.get("actor_type", "system")),
            "actor_id": str(actor.get("actor_id", "unknown")),
        },
        "command": str(event.get("command", "unknown")),
        "result": "COMMITTED",
        "occurred_at": str(event["occurred_at"]),
    }


def _task_for_instance(container: Container, instance_id: str) -> str:
    validate_identifier(instance_id, "instance_id")
    matches = [
        path.parent.parent.name
        for path in container.store.layout.control_root.glob(
            f"tasks/*/instances/{instance_id}.json"
        )
        if path.is_file()
    ]
    if len(matches) != 1:
        raise HarnessError(
            "INSTANCE_NOT_FOUND",
            "The requested instance does not resolve uniquely.",
            {"instance_id": instance_id},
        )
    return matches[0]


def _require_human(envelope: CommandEnvelope, message: str) -> None:
    if envelope.actor_type != "human":
        raise HarnessError("VALIDATION_ERROR", message)
