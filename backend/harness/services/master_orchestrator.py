"""Durable in-process Master orchestration over typed model and asset tools."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from ..domain.master import validate_plan_proposal
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from .asset_tools import AssetToolRegistry
from .model_clients import ModelClientFactory, ModelClientFailure, ToolCall
from .task_config import TaskConfigService
from .usage import UsageService

_MASTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "message", "task_title", "proposal"],
    "properties": {
        "status": {"type": "string", "enum": ["NEEDS_INPUT", "PLAN_READY"]},
        "message": {"type": "string", "minLength": 1, "maxLength": 20000},
        "task_title": {
            "oneOf": [
                {"type": "null"},
                {"type": "string", "minLength": 1, "maxLength": 256},
            ]
        },
        "proposal": {"oneOf": [{"type": "null"}, {"type": "object"}]},
    },
}


@dataclass(frozen=True, slots=True)
class MasterRunObservation:
    status: str
    message: str | None = None
    task_title: str | None = None


class MasterOrchestratorFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MasterPlanner(Protocol):
    def submit_message(self, task_id: str, message: dict[str, Any]) -> str: ...

    def observe_run(self, task_id: str, run_id: str) -> MasterRunObservation: ...

    def load_plan(self, task_id: str, run_id: str) -> dict[str, Any]: ...


class MasterOrchestrator:
    """Execute and checkpoint every Master run inside the Harness process."""

    def __init__(
        self,
        store: FileStateStore,
        contracts: ContractRegistry,
        task_config: TaskConfigService,
        model_clients: ModelClientFactory,
        asset_tools: AssetToolRegistry,
        usage: UsageService,
    ) -> None:
        self.store = store
        self.contracts = contracts
        self.task_config = task_config
        self.model_clients = model_clients
        self.asset_tools = asset_tools
        self.usage = usage

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str:
        message_id = message.get("message_id")
        if not isinstance(message_id, str) or message.get("task_id") != task_id:
            raise MasterOrchestratorFailure(
                "MASTER_RUN_FAILED", "The queued Master message is invalid."
            )
        run_id = "run_" + hashlib.sha256(f"{task_id}\0{message_id}".encode()).hexdigest()[:32]
        path = self._path(task_id, run_id)
        message_sha256 = digest_json(message)
        if path.exists():
            run = read_json(path)
            if run.get("message_sha256") != message_sha256:
                raise MasterOrchestratorFailure(
                    "MASTER_RUN_FAILED", "The durable Master run conflicts with its message."
                )
            if run.get("status") != "RUNNING":
                return run_id
        else:
            run = {
                "schema_version": "1.0",
                "run_id": run_id,
                "task_id": task_id,
                "message_id": message_id,
                "message_sha256": message_sha256,
                "status": "RUNNING",
                "message": None,
                "task_title": None,
                "proposal": None,
                "error": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            atomic_write_json(path, run, mode=0o640)
        self._execute(path, run)
        return run_id

    def observe_run(self, task_id: str, run_id: str) -> MasterRunObservation:
        run = self._load(task_id, run_id)
        return MasterRunObservation(run["status"], run["message"], run["task_title"])

    def load_plan(self, task_id: str, run_id: str) -> dict[str, Any]:
        run = self._load(task_id, run_id)
        proposal = run.get("proposal")
        if run.get("status") != "PLAN_READY" or not isinstance(proposal, dict):
            raise MasterOrchestratorFailure(
                "MASTER_RUN_FAILED", "The Master run has no ready PlanProposal."
            )
        return deepcopy(proposal)

    def _execute(self, path: Path, run: dict[str, Any]) -> None:
        task_id = run["task_id"]
        try:
            snapshot = self.task_config.resolve(task_id)
            catalog = self.asset_tools.execute(
                task_id,
                "list_assets",
                {},
                idempotency_key=f"{run['run_id']}-asset-catalog",
            )
            messages = self._messages(task_id, run, catalog)
            tools = self.asset_tools.definitions()
            client = self.model_clients.text(
                snapshot,
                snapshot.runtime.models.master,
                timeout_seconds=snapshot.runtime.master.model_timeout_seconds,
            )
            output: dict[str, Any] | None = None
            for round_index in range(snapshot.runtime.master.max_tool_rounds + 1):
                result = client.complete_structured(
                    messages=messages,
                    tools=tools,
                    response_schema=_MASTER_RESPONSE_SCHEMA,
                    idempotency_key=f"{run['run_id']}-round-{round_index}",
                )
                self.usage.record_master_model_call(task_id, result)
                if not result.tool_calls:
                    output = result.output
                    break
                if round_index >= snapshot.runtime.master.max_tool_rounds:
                    raise MasterOrchestratorFailure(
                        "MASTER_RUN_FAILED", "Master exceeded the configured tool round limit."
                    )
                messages.append(self._assistant_tool_message(result.tool_calls))
                for call in result.tool_calls:
                    tool_result = self.asset_tools.execute(
                        task_id,
                        call.name,
                        call.arguments,
                        idempotency_key=f"{run['run_id']}-{round_index}-{call.call_id}",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": json.dumps(
                                tool_result,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        }
                    )
            if output is None:
                raise MasterOrchestratorFailure(
                    "MASTER_RUN_FAILED", "Master did not return structured output."
                )
            normalized = self._validate_output(task_id, output, snapshot.runtime.master)
            run.update(
                {
                    "status": normalized["status"],
                    "message": normalized["message"],
                    "task_title": normalized["task_title"],
                    "proposal": normalized["proposal"],
                    "error": None,
                    "updated_at": utc_now(),
                }
            )
        except (ModelClientFailure, MasterOrchestratorFailure) as exc:
            run.update(
                {
                    "status": "FAILED",
                    "message": exc.message,
                    "proposal": None,
                    "error": {"code": exc.code, "message": exc.message},
                    "updated_at": utc_now(),
                }
            )
        except HarnessError as exc:
            safe_message = (
                "Master could not use the requested asset data."
                if exc.code == "VALIDATION_ERROR"
                else exc.message
            )
            run.update(
                {
                    "status": "FAILED",
                    "message": safe_message,
                    "proposal": None,
                    "error": {"code": "MASTER_RUN_FAILED", "message": safe_message},
                    "updated_at": utc_now(),
                }
            )
        atomic_write_json(path, run, mode=0o640)

    def _messages(
        self, task_id: str, run: dict[str, Any], catalog: dict[str, Any]
    ) -> list[dict[str, Any]]:
        thread = self.store.master_thread.get(task_id, task_id)
        latest_revision = 0 if thread is None else thread["latest_proposal_revision"]
        clarification_count = sum(
            1
            for item in self.store.master_message.list(task_id)
            if item["role"] == "master" and item["kind"] == "clarification"
        )
        latest_proposal = max(
            self.store.plan_proposal.list(task_id),
            key=lambda item: item["revision"],
            default=None,
        )
        system_context = {
            "task_id": task_id,
            "next_proposal_revision": latest_revision + 1,
            "clarification_questions_already_asked": clarification_count,
            "asset_catalog": catalog["assets"],
            "latest_proposal": latest_proposal,
        }
        system = (
            "You are the in-process Master planner. Clarify only when essential; otherwise "
            "produce a contract-valid PlanProposal with status PENDING_CONFIRMATION. Use only "
            "registered asset tools. Cite factual asset decisions in TaskCard instructions as "
            "asset_id/page/block_id and keep input_assets on authoritative manifests. Never invent "
            "asset contents, provider configuration, credentials, or supported Agent types. "
            "Return exactly the requested structured response. Context: "
            + json.dumps(system_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for item in sorted(
            self.store.master_message.list(task_id), key=lambda value: value["sequence"]
        ):
            if item["message_id"] == run["message_id"] or item["role"] in {"user", "master"}:
                messages.append(
                    {
                        "role": "assistant" if item["role"] == "master" else "user",
                        "content": item["content"],
                    }
                )
        return messages

    def _validate_output(
        self, task_id: str, output: dict[str, Any], master_config: Any
    ) -> dict[str, Any]:
        if set(output) != {"status", "message", "task_title", "proposal"}:
            self._invalid_output("Master returned unexpected structured fields.")
        status = output["status"]
        message = output["message"]
        title = output["task_title"]
        proposal = output["proposal"]
        if (
            status not in {"NEEDS_INPUT", "PLAN_READY"}
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > 20000
            or (
                title is not None
                and (
                    not isinstance(title, str)
                    or not title.strip()
                    or len(title) > 256
                )
            )
        ):
            self._invalid_output("Master returned malformed structured fields.")
        if status == "NEEDS_INPUT":
            clarification_count = sum(
                1
                for item in self.store.master_message.list(task_id)
                if item["role"] == "master" and item["kind"] == "clarification"
            )
            if clarification_count >= master_config.max_clarification_questions:
                self._invalid_output("Master exceeded the configured clarification limit.")
            if proposal is not None:
                self._invalid_output("A clarification response cannot include a plan.")
        else:
            if not isinstance(proposal, dict):
                self._invalid_output("A ready Master response requires a PlanProposal.")
            thread = self.store.master_thread.get(task_id, task_id)
            expected = 1 if thread is None else thread["latest_proposal_revision"] + 1
            validate_plan_proposal(
                self.contracts,
                cast(dict[str, Any], proposal),
                task_id=task_id,
                expected_revision=expected,
            )
        return {
            "status": status,
            "message": message.strip(),
            "task_title": None if title is None else title.strip(),
            "proposal": deepcopy(proposal),
        }

    @staticmethod
    def _assistant_tool_message(tool_calls: tuple[ToolCall, ...]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in tool_calls
            ],
        }

    def _load(self, task_id: str, run_id: str) -> dict[str, Any]:
        path = self._path(task_id, run_id)
        if not path.exists():
            raise MasterOrchestratorFailure(
                "MASTER_RUN_FAILED", "The durable Master run does not exist."
            )
        run = read_json(path)
        if run.get("task_id") != task_id or run.get("run_id") != run_id:
            raise MasterOrchestratorFailure(
                "MASTER_RUN_FAILED", "The durable Master run owner is invalid."
            )
        return run

    def _path(self, task_id: str, run_id: str) -> Path:
        validate_identifier(task_id, "task_id")
        validate_identifier(run_id, "run_id")
        if not run_id.startswith("run_"):
            raise MasterOrchestratorFailure("MASTER_RUN_FAILED", "Invalid Master run id.")
        root = (
            self.store.layout.control_root / "tasks" / task_id / "master" / "runs"
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root / f"{run_id}.json"

    @staticmethod
    def _invalid_output(message: str) -> None:
        raise MasterOrchestratorFailure("MASTER_OUTPUT_INVALID", message)
