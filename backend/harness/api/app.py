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
from ..contracts import ContractRegistry
from ..core.config import HarnessSettings, load_settings
from ..core.errors import ErrorCatalog, HarnessError
from ..core.logging import configure_logging, redact
from ..domain.service import TaskCommandService
from ..storage.store import FileStateStore


@dataclass(slots=True)
class Container:
    settings: HarnessSettings
    contracts: ContractRegistry
    errors: ErrorCatalog
    store: FileStateStore
    commands: TaskCommandService


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
    return Container(
        settings=settings,
        contracts=contracts,
        errors=errors,
        store=store,
        commands=TaskCommandService(store, contracts),
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
        logger.info(
            "control_plane_started",
            extra={"fields": {"recovery_warning_count": len(warnings)}},
        )
        try:
            yield
        finally:
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

    return app
