"""Real Master planning boundary with an explicit unavailable default."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class MasterRunObservation:
    status: str
    message: str | None = None
    task_title: str | None = None


class MasterGatewayFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MasterGateway(Protocol):
    @property
    def available(self) -> bool: ...

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str: ...

    def observe_run(self, run_id: str) -> MasterRunObservation: ...

    def load_plan(self, run_id: str) -> dict[str, Any]: ...

    def cancel_run(self, run_id: str) -> dict[str, Any]: ...


class UnavailableMasterGateway:
    available = False

    @staticmethod
    def _raise() -> NoReturn:
        raise MasterGatewayFailure(
            "MASTER_UNAVAILABLE",
            "未配置真实 MasterGateway, 无法分析消息或生成计划。",
        )

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str:
        self._raise()

    def observe_run(self, run_id: str) -> MasterRunObservation:
        self._raise()

    def load_plan(self, run_id: str) -> dict[str, Any]:
        self._raise()

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        self._raise()


class HttpMasterGateway:
    """HTTP adapter for a separately deployed, idempotent Master runtime."""

    available = True
    _MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str:
        payload = self._request(
            "POST",
            "/v1/runs",
            {"task_id": task_id, "message": message},
        )
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            self._invalid("MasterGateway did not return a run_id.")
        return cast(str, run_id)

    def observe_run(self, run_id: str) -> MasterRunObservation:
        payload = self._request("GET", f"/v1/runs/{quote(run_id, safe='')}")
        status = payload.get("status")
        if status not in {"RUNNING", "NEEDS_INPUT", "PLAN_READY", "FAILED"}:
            self._invalid("MasterGateway returned an unsupported run status.")
        message = payload.get("message")
        task_title = payload.get("task_title")
        if message is not None and not isinstance(message, str):
            self._invalid("MasterGateway returned an invalid message.")
        if task_title is not None and not isinstance(task_title, str):
            self._invalid("MasterGateway returned an invalid task title.")
        return MasterRunObservation(
            cast(str, status),
            cast(str | None, message),
            cast(str | None, task_title),
        )

    def load_plan(self, run_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/v1/runs/{quote(run_id, safe='')}/plan")
        proposal = payload.get("proposal", payload)
        if not isinstance(proposal, dict):
            self._invalid("MasterGateway returned an invalid PlanProposal.")
        return proposal

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/runs/{quote(run_id, safe='')}/cancel", {})

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self._MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise MasterGatewayFailure(
                "MASTER_RUN_FAILED",
                "MasterGateway 暂时不可用, 请稍后重试。",
            ) from exc
        if len(raw) > self._MAX_RESPONSE_BYTES:
            self._invalid("MasterGateway response exceeded the size limit.")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MasterGatewayFailure(
                "MASTER_RUN_FAILED", "MasterGateway returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            self._invalid("MasterGateway response must be a JSON object.")
        return payload

    @staticmethod
    def _invalid(message: str) -> None:
        raise MasterGatewayFailure("MASTER_RUN_FAILED", message)
