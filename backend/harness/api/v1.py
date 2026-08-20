"""G2 versioned HTTP use cases for tasks and one runnable Image instance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import read_json
from ..storage.layout import validate_identifier

if TYPE_CHECKING:
    from .app import Container


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(StrictRequest):
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
    async def list_tasks() -> dict[str, Any]:
        def read_index() -> dict[str, Any]:
            index = read_json(
                container.store.layout.control_root / "indexes" / "task-index.json"
            )
            return {"schema_version": "1.0", "items": index["tasks"]}

        return await run_in_threadpool(read_index)

    @router.get("/tasks/{task_id}", tags=["tasks"])
    async def get_task(task_id: str) -> dict[str, Any]:
        def read_task() -> dict[str, Any]:
            validate_identifier(task_id, "task_id")
            task = container.store.task.get(task_id, task_id)
            if task is None:
                raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
            return {
                "schema_version": "1.0",
                "task": task,
                "task_revision": container.store.task.revision(task_id, task_id),
                "plan": container.store.plan.get(task_id, task_id),
            }

        return await run_in_threadpool(read_task)

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
            return {"schema_version": "1.0", "task_id": task_id, **result}

        return await run_in_threadpool(read_instance)

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

    @router.get("/adapters", tags=["instances"])
    async def list_adapters() -> dict[str, Any]:
        return {"schema_version": "1.0", "items": container.adapters.describe()}

    return router


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
