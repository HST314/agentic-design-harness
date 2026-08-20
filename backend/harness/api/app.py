"""FastAPI application factory, lifecycle and foundation endpoints."""

from __future__ import annotations

import logging
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
from ..services.supervisor import ProcessSupervisor
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
    supervisor: ProcessSupervisor
    adapters: AdapterRegistry
    application: HarnessApplicationService


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
    application = HarnessApplicationService(
        store,
        commands,
        assets,
        approvals,
        credentials,
        supervisor,
        adapters,
    )
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
        supervisor=supervisor,
        adapters=adapters,
        application=application,
    )


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
        asset_recoveries = container.assets.recover()
        process_recoveries = container.supervisor.reconcile()
        application_recoveries = container.application.recover()
        global_config = container.configuration.get_global()
        assert global_config is not None
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
                    "asset_recovery_count": len(asset_recoveries),
                    "application_recovery_count": len(application_recoveries),
                    "process_recovery_count": len(process_recoveries),
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
        version=__version__,
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
