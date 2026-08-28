"""Durable in-process Master orchestration over typed model and asset tools."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core.config_kernel import MasterConfig
from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from .asset_tools import AssetToolRegistry
from .model_clients import (
    ModelClientFactory,
    ModelClientFailure,
    TextModelClient,
    ToolCall,
)
from .plan_drafts import (
    PlanDraftValidationError,
    master_response_schema,
    materialize_plan_draft,
    validate_master_response,
)
from .plan_proposals import PlanProposalValidationService
from .task_config import TaskConfigService
from .usage import UsageService

_MAX_VALIDATION_REPAIR_ATTEMPTS = 2
_INVALID_PLAN_MESSAGE = "Master 生成的计划未通过结构校验, 请重新发送要求后重试。"
_ASSET_CATALOG_MESSAGE = "Master 无法读取任务素材, 请检查素材后重试。"
_ASSET_TOOL_MESSAGE = "Master 无法读取所选素材内容, 请检查素材后重试。"
_PLAN_REASON_BY_MESSAGE = {
    (
        "Every input asset in a Master execution card requires an asset_id/page or "
        "asset_id/block source citation."
    ): "missing_source_citation",
    "A source citation references an undeclared input asset.": "undeclared_source_citation",
    "A cited input asset has no persisted source understanding.": "source_understanding_missing",
    "A source citation references a page that does not exist.": "source_page_not_found",
    "A source citation references a block that does not exist.": "source_block_not_found",
}


@dataclass(frozen=True, slots=True)
class MasterRunObservation:
    status: str
    message: str | None = None
    task_title: str | None = None
    error_code: str | None = None


class MasterOrchestratorFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostic = diagnostic


class MasterPlanner(Protocol):
    def submit_message(self, task_id: str, message: dict[str, Any]) -> str: ...

    def execute_run(self, task_id: str, run_id: str) -> None: ...

    def observe_run(self, task_id: str, run_id: str) -> MasterRunObservation: ...

    def load_plan(self, task_id: str, run_id: str) -> dict[str, Any]: ...


class MasterOrchestrator:
    """Execute and checkpoint every Master run inside the Harness process."""

    def __init__(
        self,
        store: FileStateStore,
        task_config: TaskConfigService,
        model_clients: ModelClientFactory,
        asset_tools: AssetToolRegistry,
        usage: UsageService,
        plan_proposals: PlanProposalValidationService,
    ) -> None:
        self.store = store
        self.task_config = task_config
        self.model_clients = model_clients
        self.asset_tools = asset_tools
        self.usage = usage
        self.plan_proposals = plan_proposals

    def submit_message(self, task_id: str, message: dict[str, Any]) -> str:
        """Persist an idempotent run without performing slow model work."""

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
            return run_id
        else:
            run = {
                "schema_version": "1.0",
                "run_id": run_id,
                "task_id": task_id,
                "message_id": message_id,
                "message_sha256": message_sha256,
                "status": "QUEUED",
                "message": None,
                "task_title": None,
                "proposal": None,
                "error": None,
                "diagnostic": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            atomic_write_json(path, run, mode=0o640)
        return run_id

    def execute_run(self, task_id: str, run_id: str) -> None:
        """Run one persisted Master operation outside the task command lock."""

        path = self._path(task_id, run_id)
        run = self._load(task_id, run_id)
        if run["status"] not in {"QUEUED", "RUNNING"}:
            return
        if run["status"] == "QUEUED":
            run.update({"status": "RUNNING", "updated_at": utc_now()})
            atomic_write_json(path, run, mode=0o640)
        self._execute(path, run)

    def observe_run(self, task_id: str, run_id: str) -> MasterRunObservation:
        run = self._load(task_id, run_id)
        error = run.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        return MasterRunObservation(run["status"], run["message"], run["task_title"], error_code)

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
            try:
                catalog = self.asset_tools.execute(
                    task_id,
                    "list_assets",
                    {},
                    idempotency_key=f"{run['run_id']}-asset-catalog",
                )
            except HarnessError as exc:
                raise MasterOrchestratorFailure(
                    "MASTER_ASSET_CATALOG_FAILED",
                    _ASSET_CATALOG_MESSAGE,
                    self._diagnostic(
                        phase="asset_catalog",
                        cause_code=exc.code,
                        reason="asset_catalog_failed",
                    ),
                ) from exc
            asset_ids = self._asset_ids(catalog)
            messages = self._messages(task_id, run, catalog)
            tools = self.asset_tools.definitions() if asset_ids else []
            response_schema = master_response_schema(asset_ids)
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
                    response_schema=response_schema,
                    idempotency_key=f"{run['run_id']}-round-{round_index}",
                )
                self.usage.record_master_model_call(task_id, result)
                if not result.tool_calls:
                    output = result.output
                    break
                if round_index >= snapshot.runtime.master.max_tool_rounds:
                    raise MasterOrchestratorFailure(
                        "MASTER_OUTPUT_INVALID",
                        _INVALID_PLAN_MESSAGE,
                        self._diagnostic(
                            phase="model_tool_loop",
                            cause_code="MASTER_TOOL_ROUND_LIMIT",
                            reason="max_tool_rounds",
                        ),
                    )
                if not tools:
                    raise MasterOrchestratorFailure(
                        "MASTER_ASSET_TOOL_FAILED",
                        _ASSET_TOOL_MESSAGE,
                        self._diagnostic(
                            phase="asset_tool",
                            cause_code="ASSET_TOOLS_UNAVAILABLE",
                            reason="empty_asset_catalog",
                        ),
                    )
                messages.append(self._assistant_tool_message(result.tool_calls))
                for call in result.tool_calls:
                    try:
                        tool_result = self.asset_tools.execute(
                            task_id,
                            call.name,
                            call.arguments,
                            idempotency_key=(f"{run['run_id']}-{round_index}-{call.call_id}"),
                        )
                    except HarnessError as exc:
                        raise MasterOrchestratorFailure(
                            "MASTER_ASSET_TOOL_FAILED",
                            _ASSET_TOOL_MESSAGE,
                            self._diagnostic(
                                phase="asset_tool",
                                cause_code=exc.code,
                                reason="asset_tool_failed",
                            ),
                        ) from exc
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
            normalized = self._validate_with_repairs(
                task_id,
                output,
                snapshot.runtime.master,
                created_at=run["created_at"],
                asset_ids=asset_ids,
                client=client,
                messages=messages,
                response_schema=response_schema,
                run_id=run["run_id"],
            )
            run.update(
                {
                    "status": normalized["status"],
                    "message": normalized["message"],
                    "task_title": normalized["task_title"],
                    "proposal": normalized["proposal"],
                    "error": None,
                    "diagnostic": None,
                    "updated_at": utc_now(),
                }
            )
        except ModelClientFailure as exc:
            code = "MASTER_OUTPUT_INVALID" if exc.code == "MODEL_OUTPUT_INVALID" else exc.code
            message = _INVALID_PLAN_MESSAGE if code == "MASTER_OUTPUT_INVALID" else exc.message
            run.update(
                {
                    "status": "FAILED",
                    "message": message,
                    "proposal": None,
                    "error": {"code": code, "message": message},
                    "diagnostic": self._diagnostic(
                        phase=(
                            "model_output" if code == "MASTER_OUTPUT_INVALID" else "model_provider"
                        ),
                        cause_code=exc.code,
                        reason=exc.code.lower(),
                    ),
                    "updated_at": utc_now(),
                }
            )
        except MasterOrchestratorFailure as exc:
            run.update(
                {
                    "status": "FAILED",
                    "message": exc.message,
                    "proposal": None,
                    "error": {"code": exc.code, "message": exc.message},
                    "diagnostic": exc.diagnostic,
                    "updated_at": utc_now(),
                }
            )
        except HarnessError as exc:
            safe_message = "Master 编排服务未能完成本次计划, 请稍后重试。"
            run.update(
                {
                    "status": "FAILED",
                    "message": safe_message,
                    "proposal": None,
                    "error": {"code": "MASTER_RUN_FAILED", "message": safe_message},
                    "diagnostic": self._diagnostic(
                        phase="orchestration",
                        cause_code=exc.code,
                        reason="orchestration_failure",
                    ),
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
            "produce a contract-valid semantic PlanDraft. The server owns every durable id, "
            "task id, revision, status, timestamp and manifest path; never generate those fields. "
            "Use only registered asset tools. Cite factual asset decisions in stage "
            "instructions as "
            "asset_id/page/<page_number> or asset_id/block/<block_id>, and keep input_assets on "
            "authoritative catalog ids. Never invent asset contents, provider configuration, "
            "credentials, or supported Agent types. Use a general stage for file-oriented or "
            "general reasoning work that does not require image generation or presentation "
            "authoring; its parameters must be empty. Return exactly the requested structured "
            "response. Plan one stage per independent deliverable: first list the standalone "
            "deliverables the prompt asks for, then emit exactly one image stage per deliverable "
            "when that deliverable is visual, each with its own distinct title, inside a single "
            "PlanDraft "
            "submitted at once. Merge deliverables into one stage only when they truly "
            "cannot be delivered independently. Never emit more than 6 image stages in a "
            "single plan. "
            + (
                "This task has no assets: every input_asset_ids value must be an empty list, "
                "and no instruction may claim or cite an asset. "
                if not catalog["assets"]
                else "Every input_asset_ids value must contain only ids from asset_catalog. "
            )
            + "Context: "
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

    def _validate_with_repairs(
        self,
        task_id: str,
        output: dict[str, Any] | None,
        master_config: MasterConfig,
        *,
        created_at: str,
        asset_ids: list[str],
        client: TextModelClient,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        repair_messages = deepcopy(messages)
        candidate: dict[str, Any] | None = output
        for repair_index in range(_MAX_VALIDATION_REPAIR_ATTEMPTS + 1):
            try:
                return self._validate_output(
                    task_id,
                    candidate,
                    master_config,
                    created_at=created_at,
                    asset_ids=asset_ids,
                    response_schema=response_schema,
                )
            except MasterOrchestratorFailure as exc:
                if (
                    exc.code != "MASTER_OUTPUT_INVALID"
                    or repair_index >= _MAX_VALIDATION_REPAIR_ATTEMPTS
                ):
                    raise
                repair_messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": json.dumps(
                                candidate if candidate is not None else {},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                        {
                            "role": "user",
                            "content": self._repair_prompt(exc.diagnostic),
                        },
                    ]
                )
                result = client.complete_structured(
                    messages=repair_messages,
                    tools=[],
                    response_schema=response_schema,
                    idempotency_key=f"{run_id}-validation-repair-{repair_index + 1}",
                )
                self.usage.record_master_model_call(task_id, result)
                candidate = None if result.tool_calls else result.output
        raise AssertionError("validation repair loop must return or raise")

    def _validate_output(
        self,
        task_id: str,
        output: dict[str, Any] | None,
        master_config: MasterConfig,
        *,
        created_at: str,
        asset_ids: list[str],
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(output, dict):
            raise self._invalid_output(
                phase="model_output",
                schema="master-response",
                path="$",
                reason="type",
                output=output,
            )
        try:
            validate_master_response(response_schema, output)
        except PlanDraftValidationError as exc:
            raise self._invalid_output(
                phase="plan_draft_validation",
                schema=exc.schema,
                path=exc.path,
                reason=exc.reason,
                output=output,
            ) from exc
        status = output["status"]
        message = output["message"]
        title = output["task_title"]
        draft = output["plan_draft"]
        if not message.strip() or (isinstance(title, str) and not title.strip()):
            raise self._invalid_output(
                phase="plan_draft_validation",
                schema="master-response",
                path="message" if not message.strip() else "task_title",
                reason="blank_text",
                output=output,
            )
        if status == "NEEDS_INPUT":
            clarification_count = sum(
                1
                for item in self.store.master_message.list(task_id)
                if item["role"] == "master" and item["kind"] == "clarification"
            )
            if clarification_count >= master_config.max_clarification_questions:
                raise self._invalid_output(
                    phase="plan_draft_validation",
                    schema="master-response",
                    path="status",
                    reason="clarification_limit",
                    output=output,
                )
            if draft is not None:
                raise self._invalid_output(
                    phase="plan_draft_validation",
                    schema="master-response",
                    path="plan_draft",
                    reason="clarification_with_plan",
                    output=output,
                )
            proposal = None
        else:
            if not isinstance(draft, dict):
                raise self._invalid_output(
                    phase="plan_draft_validation",
                    schema="master-response",
                    path="plan_draft",
                    reason="plan_required",
                    output=output,
                )
            thread = self.store.master_thread.get(task_id, task_id)
            expected = 1 if thread is None else thread["latest_proposal_revision"] + 1
            try:
                proposal = materialize_plan_draft(
                    task_id,
                    expected,
                    draft,
                    created_at=created_at,
                    asset_ids=set(asset_ids),
                )
            except PlanDraftValidationError as exc:
                raise self._invalid_output(
                    phase="plan_materialization",
                    schema=exc.schema,
                    path=exc.path,
                    reason=exc.reason,
                    output=output,
                ) from exc
            try:
                self.plan_proposals.validate_new(
                    task_id,
                    proposal,
                    expected_revision=expected,
                )
            except HarnessError as exc:
                details = exc.details if isinstance(exc.details, dict) else {}
                raise self._invalid_output(
                    phase="plan_proposal_validation",
                    schema=str(details.get("schema", "plan-proposal")),
                    path=str(details.get("path", "$")),
                    reason=str(
                        details.get(
                            "reason",
                            _PLAN_REASON_BY_MESSAGE.get(exc.message, "semantic_validation"),
                        )
                    ),
                    output=output,
                ) from exc
        return {
            "status": status,
            "message": message.strip(),
            "task_title": None if title is None else title.strip(),
            "proposal": proposal,
        }

    @staticmethod
    def _asset_ids(catalog: dict[str, Any]) -> list[str]:
        assets = catalog.get("assets") if isinstance(catalog, dict) else None
        if not isinstance(assets, list):
            raise MasterOrchestratorFailure(
                "MASTER_ASSET_CATALOG_FAILED",
                _ASSET_CATALOG_MESSAGE,
                MasterOrchestrator._diagnostic(
                    phase="asset_catalog",
                    cause_code="ASSET_CATALOG_INVALID",
                    reason="invalid_catalog_shape",
                ),
            )
        asset_ids: list[str] = []
        for item in assets:
            asset_id = item.get("asset_id") if isinstance(item, dict) else None
            if not isinstance(asset_id, str) or not asset_id:
                raise MasterOrchestratorFailure(
                    "MASTER_ASSET_CATALOG_FAILED",
                    _ASSET_CATALOG_MESSAGE,
                    MasterOrchestrator._diagnostic(
                        phase="asset_catalog",
                        cause_code="ASSET_CATALOG_INVALID",
                        reason="invalid_catalog_assets",
                    ),
                )
            asset_ids.append(asset_id)
        if len(set(asset_ids)) != len(asset_ids):
            raise MasterOrchestratorFailure(
                "MASTER_ASSET_CATALOG_FAILED",
                _ASSET_CATALOG_MESSAGE,
                MasterOrchestrator._diagnostic(
                    phase="asset_catalog",
                    cause_code="ASSET_CATALOG_INVALID",
                    reason="invalid_catalog_assets",
                ),
            )
        return asset_ids

    @staticmethod
    def _repair_prompt(diagnostic: dict[str, Any] | None) -> str:
        safe = diagnostic or {
            "phase": "plan_draft_validation",
            "cause_code": "MASTER_OUTPUT_INVALID",
            "schema": "master-response",
            "path": "$",
            "reason": "validation_failed",
            "output_sha256": None,
        }
        return (
            "The previous response failed server-side validation. Return one complete "
            "replacement response that satisfies the supplied JSON Schema. Do not call tools "
            "and do not add durable ids, revisions, statuses, timestamps or manifest paths to "
            "the PlanDraft. Safe validation diagnostic: "
            + json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _diagnostic(
        *,
        phase: str,
        cause_code: str,
        schema: str | None = None,
        path: str | None = None,
        reason: str | None = None,
        output: Any = None,
        has_output: bool = False,
    ) -> dict[str, Any]:
        return {
            "phase": phase[:64],
            "cause_code": cause_code[:128],
            "schema": None if schema is None else schema[:128],
            "path": None if path is None else path[:512],
            "reason": None if reason is None else reason[:512],
            "output_sha256": digest_json(output) if has_output else None,
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
        root = self.store.layout.control_root / "tasks" / task_id / "master" / "runs"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root / f"{run_id}.json"

    @staticmethod
    def _invalid_output(
        *,
        phase: str,
        schema: str,
        path: str,
        reason: str,
        output: Any,
    ) -> MasterOrchestratorFailure:
        return MasterOrchestratorFailure(
            "MASTER_OUTPUT_INVALID",
            _INVALID_PLAN_MESSAGE,
            MasterOrchestrator._diagnostic(
                phase=phase,
                cause_code="MASTER_OUTPUT_INVALID",
                schema=schema,
                path=path,
                reason=reason,
                output=output,
                has_output=True,
            ),
        )
