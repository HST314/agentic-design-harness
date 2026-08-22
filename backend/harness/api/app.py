"""FastAPI application factory, lifecycle and foundation endpoints."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .. import __version__
from ..adapters import AdapterRegistry, ImageAgentAdapter, PptAgentContractAdapter
from ..contracts import ContractRegistry
from ..core.config import HarnessSettings, load_settings
from ..core.errors import ErrorCatalog, HarnessError
from ..core.logging import configure_logging, redact
from ..domain.service import TaskCommandService
from ..services.application import HarnessApplicationService
from ..services.approvals import ApprovalInboxService
from ..services.assets import AssetService
from ..services.configuration import ConfigurationService
from ..services.credentials import CredentialPoolService
from ..services.retry_budget import RetryBudgetService
from ..services.supervisor import ProcessSupervisor
from ..services.task_intakes import TaskIntakeService
from ..services.usage import UsageService
from ..storage.atomic import read_json
from ..storage.store import FileStateStore
from .v1 import build_v1_router


@dataclass(slots=True)
class Container:
    settings: HarnessSettings
    contracts: ContractRegistry
    errors: ErrorCatalog
    store: FileStateStore
    commands: TaskCommandService
    assets: AssetService
    approvals: ApprovalInboxService
    credentials: CredentialPoolService
    configuration: ConfigurationService
    usage: UsageService
    retry_budgets: RetryBudgetService
    supervisor: ProcessSupervisor
    adapters: AdapterRegistry
    application: HarnessApplicationService
    task_intakes: TaskIntakeService


class ContractValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]


def build_container(settings: HarnessSettings) -> Container:
    contracts = ContractRegistry(settings.contracts_root)
    errors = ErrorCatalog(settings.contracts_root / "catalogs" / "error-codes.json")
    store = FileStateStore(
        settings.control_root,
        settings.workspace_root,
        contracts,
        settings.lock_timeout_seconds,
    )
    commands = TaskCommandService(store, contracts)
    assets = AssetService(store)
    approvals = ApprovalInboxService(store)
    credentials = CredentialPoolService(store)
    configuration = ConfigurationService(store)
    usage = UsageService(store)
    retry_budgets = RetryBudgetService(store, approvals)
    supervisor = ProcessSupervisor(
        store,
        commands,
        credentials,
        configuration,
        host=settings.host,
    )
    adapters = AdapterRegistry(
        [
            ImageAgentAdapter(
                store,
                contracts,
                assets,
                configuration,
                source_root=settings.image_agent_root,
                interpreter=settings.image_agent_python,
                dependency_root=settings.image_agent_dependency_root,
                revision=settings.image_agent_revision,
                host=settings.host,
            ),
            PptAgentContractAdapter(),
        ]
    )

    def apply_live_config(task_id: str, instance_id: str, snapshot_path: Path) -> bool:
        instance = store.instance.get(task_id, instance_id)
        if instance is None:
            return False
        adapter = adapters.get(instance["agent_type"])
        snapshot = read_json(snapshot_path)
        result = adapter.apply_config(
            instance_id,
            snapshot,
            snapshot["config_revision"],
            f"config_{instance_id}_{snapshot['config_revision']}",
        )
        return result.accepted

    configuration.apply_config = apply_live_config
    application = HarnessApplicationService(
        store,
        commands,
        assets,
        approvals,
        credentials,
        supervisor,
        adapters,
        configuration,
    )
    task_intakes = TaskIntakeService(store, commands, assets)
    return Container(
        settings=settings,
        contracts=contracts,
        errors=errors,
        store=store,
        commands=commands,
        assets=assets,
        approvals=approvals,
        credentials=credentials,
        configuration=configuration,
        usage=usage,
        retry_budgets=retry_budgets,
        supervisor=supervisor,
        adapters=adapters,
        application=application,
        task_intakes=task_intakes,
    )


def recover_adapters(container: Container) -> list[dict[str, Any]]:
    """Reconcile live Agent protocol state without replaying commands."""

    recovered: list[dict[str, Any]] = []
    tasks_root = container.store.layout.control_root / "tasks"
    for task_dir in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        plan = container.store.plan.get(task_id, task_id)
        if plan is None:
            continue
        for instance in plan["instances"]:
            if instance["status"] not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
                continue
            adapter = container.adapters.get(instance["agent_type"])
            result = adapter.recover(instance)
            recovered.append(
                {
                    "task_id": task_id,
                    "instance_id": instance["instance_id"],
                    "recovered": result.recovered,
                    "status": result.status,
                }
            )
    return recovered


def create_app(settings: HarnessSettings | None = None) -> FastAPI:
    project_root = Path(__file__).resolve().parents[3]
    resolved = settings or load_settings(project_root)
    configure_logging(resolved.log_level)
    container = build_container(resolved)
    logger = logging.getLogger("harness.lifecycle")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        warnings = container.store.start()
        container.configuration.initialize()
        credential_recoveries = container.credentials.recover()
        config_recoveries = container.configuration.recover()
        usage_recoveries = container.usage.recover()
        retry_budget_recoveries = container.retry_budgets.recover()
        asset_recoveries = container.assets.recover()
        process_recoveries = container.supervisor.reconcile()
        application_recoveries = container.application.recover()
        adapter_recoveries = recover_adapters(container)
        global_config = container.configuration.get_global()
        if global_config is None:
            raise HarnessError(
                "VALIDATION_ERROR", "The global configuration did not initialize."
            )
        container.supervisor.start_monitoring(
            global_config["supervisor"]["health_interval_seconds"]
        )
        logger.info(
            "control_plane_started",
            extra={
                "fields": {
                    "recovery_warning_count": len(warnings),
                    "credential_recovery_count": len(credential_recoveries),
                    "config_recovery_count": len(config_recoveries),
                    "usage_recovery_count": len(usage_recoveries),
                    "retry_budget_recovery_count": len(retry_budget_recoveries),
                    "asset_recovery_count": len(asset_recoveries),
                    "application_recovery_count": len(application_recoveries),
                    "process_recovery_count": len(process_recoveries),
                    "adapter_recovery_count": len(adapter_recoveries),
                }
            },
        )
        try:
            yield
        finally:
            container.supervisor.close()
            container.store.close()
            logger.info("control_plane_stopped")

    app = FastAPI(
        title="Agentic Design Harness API",
        description=(
            "Versioned Phase 1 control-plane API. Mutations require a command envelope "
            "with actor, idempotency key and expected revision; Agent secrets are never returned."
        ),
        version=__version__,
        openapi_tags=[
            {"name": "tasks", "description": "Main-task planning and lifecycle."},
            {"name": "instances", "description": "Isolated Agent process lifecycle."},
            {"name": "assets", "description": "Controlled import, preview and delivery."},
            {"name": "approvals", "description": "Frozen-owner workflow decisions."},
            {"name": "inbox", "description": "FIFO notifications and handling state."},
            {"name": "usage", "description": "Token, cost and retry-budget accounting."},
            {"name": "configuration", "description": "Redacted configuration boundaries."},
            {"name": "audit", "description": "Read-only public audit projection."},
        ],
        servers=[{"url": "http://127.0.0.1:18080", "description": "Local control plane"}],
        lifespan=lifespan,
    )
    app.state.container = container

    @app.exception_handler(HarnessError)
    async def handle_harness_error(_: Request, exc: HarnessError) -> JSONResponse:
        definition = container.errors.get(exc.code)
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "error": {
                "code": definition.code,
                "message": exc.message,
                "retryable": definition.retryable,
                "details": redact(exc.details),
            },
        }
        if exc.trace_id:
            payload["error"]["trace_id"] = exc.trace_id
        container.contracts.validate("error-response", payload)
        return JSONResponse(status_code=definition.http_status, content=payload)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        error = HarnessError(
            "VALIDATION_ERROR",
            "Request validation failed.",
            {"error_count": len(exc.errors())},
        )
        return await handle_harness_error(_, error)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        trace_id = f"trace_{uuid.uuid4().hex}"
        logger.exception(
            "unhandled_request_error",
            extra={"fields": {"trace_id": trace_id, "error_type": type(exc).__name__}},
        )
        return await handle_harness_error(
            _,
            HarnessError(
                "INTERNAL_ERROR",
                "The control plane could not complete the request.",
                trace_id=trace_id,
            ),
        )

    @app.middleware("http")
    async def add_control_plane_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz", tags=["foundation"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["foundation"])
    async def readiness() -> JSONResponse:
        ready = container.store.ready
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )

    @app.post("/api/v1/contracts/{schema_name}/validate", tags=["foundation"])
    async def validate_contract(
        schema_name: str, body: ContractValidationRequest
    ) -> dict[str, Any]:
        container.contracts.validate(schema_name, body.payload)
        return {"schema_version": "1.0", "valid": True, "schema": schema_name}

    app.include_router(build_v1_router(container))

    return app
