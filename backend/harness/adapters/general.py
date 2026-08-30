"""Adapter for the managed general-purpose chat Agent."""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from ..services.asset_files import decode_local_text
from ..services.process_runtime import AgentRuntimeArtifact, ProcessSpec
from ..services.task_config import TaskConfigService
from ..storage.atomic import atomic_write_bytes, atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.store import FileStateStore
from .base import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    PrepareRequest,
    ValidationResult,
)
from .image_runtime import content_tree_sha256
from .types import AgentInstanceSnapshot, DeliveryCandidate, TaskCard, UsageEvent

_GENERAL_INPUT_MAX_BYTES = 2 * 1024 * 1024


class GeneralAgentAdapter:
    agent_type = "general"
    available = True

    def __init__(
        self,
        store: FileStateStore,
        contracts: ContractRegistry,
        task_config: TaskConfigService,
        *,
        source_root: Path,
        interpreter: Path,
        host: str = "127.0.0.1",
    ) -> None:
        self.store = store
        self.contracts = contracts
        self.task_config = task_config
        self.source_root = source_root.resolve()
        self.interpreter = interpreter.resolve()
        self.host = host
        if (
            not self.source_root.is_dir()
            or self.source_root.is_symlink()
            or not (self.source_root / "app.py").is_file()
            or not (self.source_root / "runtime.json").is_file()
            or not self.interpreter.is_file()
        ):
            raise HarnessError(
                "ADAPTER_UNAVAILABLE", "The managed General Agent runtime is incomplete."
            )
        self.runtime_root = self._prepare_runtime_artifact()

    def validate_task_card(self, card: TaskCard) -> ValidationResult:
        try:
            self.contracts.validate("task-card-v1.1", card)
        except (HarnessError, ValueError) as exc:
            return ValidationResult(False, (str(exc),))
        errors = [] if card.get("agent_type") == self.agent_type else [
            "Task card agent_type must be general."
        ]
        return ValidationResult(not errors, tuple(errors))

    def prepare(self, request: PrepareRequest) -> ProcessSpec:
        validation = self.validate_task_card(request.task_card)
        if not validation.valid:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The General Agent adapter rejected its task card.",
                {"errors": list(validation.errors)},
            )
        task_id = str(request.instance["task_id"])
        instance_id = str(request.instance["instance_id"])
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        shared_root = request.task_root / "resources" / "shared"
        shared_root.mkdir(parents=True, exist_ok=True)
        materialized_inputs = self._materialize_inputs(
            request.task_root, shared_root, request.task_card
        )
        managed_state_root = instance_root / "runtime" / "general-agent-state"
        managed_state_root.mkdir(parents=True, exist_ok=True)
        if (
            managed_state_root.is_symlink()
            or not managed_state_root.is_dir()
            or not managed_state_root.resolve(strict=True).is_relative_to(
                instance_root.resolve(strict=True)
            )
        ):
            raise HarnessError(
                "PATH_OUTSIDE_TASK_ROOT", "The General Agent state directory is unsafe."
            )
        card_path = instance_root / "runtime" / "general-task-card.json"
        runtime_card = {**dict(request.task_card), "materialized_inputs": materialized_inputs}
        atomic_write_json(card_path, runtime_card, mode=0o640)
        state_path = self._state_path(task_id, instance_id)
        expected = {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "task_card_sha256": digest_json(request.task_card),
            "operation_id": None,
            "chat_state_name": f"state_{secrets.token_hex(16)}.json",
        }
        if state_path.exists():
            current = read_json(state_path)
            immutable = ("task_id", "instance_id", "task_card_sha256")
            if any(current.get(key) != expected[key] for key in immutable):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The prepared General Agent no longer matches its task card.",
                )
            chat_state_name = current.get("chat_state_name")
            if (
                not isinstance(chat_state_name, str)
                or not chat_state_name.startswith("state_")
                or not chat_state_name.endswith(".json")
                or len(chat_state_name) != 43
            ):
                raise HarnessError(
                    "PROCESS_START_FAILED", "The General Agent chat state is invalid."
                )
            expected["chat_state_name"] = chat_state_name
        else:
            atomic_write_json(state_path, expected)
        chat_state_path = managed_state_root / expected["chat_state_name"]
        self._migrate_legacy_chat_state(shared_root, managed_state_root, chat_state_path)
        snapshot = self.task_config.resolve(task_id)
        model_id = snapshot.runtime.models.text_reasoning
        model = next(
            (item for item in snapshot.model_list.text_models if item.id == model_id),
            None,
        )
        if model is None or "tool_calling" not in model.capabilities:
            raise HarnessError(
                "MODEL_CAPABILITY_UNSUPPORTED",
                "The General Agent requires the configured text model to support tool calls.",
            )
        entrypoint = self.runtime_root / "app.py"
        runtime_config = self.runtime_root / "runtime.json"
        return ProcessSpec(
            command=(
                str(self.interpreter),
                str(entrypoint),
                "--host",
                "{host}",
                "--port",
                "{port}",
            ),
            runtime_artifact=AgentRuntimeArtifact(
                artifact_id="general-agent",
                revision="1.0.0",
                source_root=self.runtime_root,
                entrypoint_relpath="app.py",
                dependency_lock_relpaths=("runtime.json",),
                environment_root=self.interpreter.parent.parent,
            ),
            public_environment={
                "GENERAL_AGENT_TASK_CARD": str(card_path),
                "GENERAL_AGENT_SHARED_ROOT": str(shared_root.resolve()),
                "GENERAL_AGENT_STATE_PATH": str(chat_state_path.resolve()),
                "GENERAL_AGENT_MODEL": model.model,
                "GENERAL_AGENT_TIMEOUT_SECONDS": str(snapshot.runtime.master.model_timeout_seconds),
                "GENERAL_AGENT_MAX_TOOL_ROUNDS": str(snapshot.runtime.master.max_tool_rounds),
                "GENERAL_AGENT_RUNTIME_CONFIG": str(runtime_config),
                "GENERAL_AGENT_MODEL_CONFIG": str(runtime_config),
            },
            writable_roots=(shared_root.resolve(), managed_state_root.resolve()),
            health_path="/healthz",
            readiness_path="/readyz",
            ui_path="/",
        )

    def _materialize_inputs(
        self, task_root: Path, shared_root: Path, card: TaskCard
    ) -> list[dict[str, str]]:
        """Copy only TaskCard-authorized inputs into the General Agent sandbox."""

        mappings: list[dict[str, str]] = []
        for reference in card["input_assets"]:
            asset_id = str(reference["asset_id"])
            validate_identifier(asset_id, "asset_id")
            expected_manifest = f"inputs/manifests/{asset_id}.json"
            if reference["manifest_relpath"] != expected_manifest:
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input manifest is not authorized.",
                    {"asset_id": asset_id},
                )
            manifest_path = task_root / expected_manifest
            try:
                manifest = read_json(manifest_path)
                self.contracts.validate("asset-manifest", manifest)
            except (OSError, ValueError, HarnessError) as exc:
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input manifest is unavailable.",
                    {"asset_id": asset_id},
                ) from exc
            if manifest["task_id"] != card["task_id"] or manifest["asset_id"] != asset_id:
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input manifest has the wrong owner.",
                    {"asset_id": asset_id},
                )
            selected_root = task_root / "inputs" / "selected" / asset_id
            selected_parent = task_root / "inputs" / "selected"
            try:
                if selected_root.is_symlink() or not selected_root.resolve(
                    strict=True
                ).is_relative_to(selected_parent.resolve(strict=True)):
                    raise OSError
                candidates = [
                    path
                    for path in selected_root.iterdir()
                    if path.is_file() and not path.is_symlink()
                ]
            except OSError:
                candidates = []
            if len(candidates) != 1:
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input file is unavailable.",
                    {"asset_id": asset_id},
                )
            source = candidates[0]
            if manifest["size_bytes"] > _GENERAL_INPUT_MAX_BYTES:
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input file exceeds its readable size limit.",
                    {"asset_id": asset_id},
                )
            raw = source.read_bytes()
            if (
                len(raw) != manifest["size_bytes"]
                or hashlib.sha256(raw).hexdigest() != manifest["sha256"]
            ):
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input file failed integrity verification.",
                    {"asset_id": asset_id},
                )
            asset_root = shared_root / "inputs" / asset_id
            asset_root.mkdir(parents=True, exist_ok=True)
            if asset_root.is_symlink() or not asset_root.resolve(strict=True).is_relative_to(
                shared_root.resolve(strict=True)
            ):
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent input destination is unsafe.",
                    {"asset_id": asset_id},
                )
            if (
                str(manifest["mime_type"]).startswith("text/")
                or manifest["mime_type"] == "application/json"
            ):
                try:
                    text = decode_local_text(raw)
                except UnicodeDecodeError as exc:
                    raise HarnessError(
                        "INPUT_ASSET_NOT_MATERIALIZED",
                        "The General Agent input text encoding is unsupported.",
                        {"asset_id": asset_id},
                    ) from exc
                destination = asset_root / source.name
                atomic_write_bytes(destination, text.encode("utf-8"), mode=0o640)
                mappings.append(
                    {
                        "asset_id": asset_id,
                        "path": destination.relative_to(shared_root).as_posix(),
                        "mime_type": str(manifest["mime_type"]),
                    }
                )
            understanding = task_root / "inputs" / "understanding" / f"{asset_id}.json"
            if (
                understanding.is_file()
                and not understanding.is_symlink()
                and understanding.stat().st_size <= _GENERAL_INPUT_MAX_BYTES
            ):
                document = read_json(understanding)
                understanding_path = asset_root / "understanding.json"
                atomic_write_json(understanding_path, document, mode=0o640)
                mappings.append(
                    {
                        "asset_id": asset_id,
                        "path": understanding_path.relative_to(shared_root).as_posix(),
                        "mime_type": "application/json",
                    }
                )
            if not any(item["asset_id"] == asset_id for item in mappings):
                raise HarnessError(
                    "INPUT_ASSET_NOT_MATERIALIZED",
                    "The General Agent has no readable representation of an input asset.",
                    {"asset_id": asset_id},
                )
        return mappings

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult:
        validate_identifier(operation_id, "operation_id")
        task_id = self._task_id_for_instance(instance_id)
        state = read_json(self._state_path(task_id, instance_id))
        state["operation_id"] = operation_id
        atomic_write_json(self._state_path(task_id, instance_id), state)
        return AdapterCommandResult(True, operation_id, {"chat_initialized": True})

    def stop(self, instance_id: str, reason: str, operation_id: str) -> AdapterCommandResult:
        del reason
        validate_identifier(operation_id, "operation_id")
        self._task_id_for_instance(instance_id)
        return AdapterCommandResult(True, operation_id)

    def get_status(self, instance_id: str) -> AdapterObservation:
        task_id = self._task_id_for_instance(instance_id)
        instance = self.store.instance.get(task_id, instance_id)
        process = None if instance is None else instance.get("process")
        running = isinstance(process, dict) and process.get("state") == "RUNNING"
        status = "RUNNING" if running else "FAILED"
        return AdapterObservation(
            status,
            step_id="chat",
            capabilities=("read_file", "write_file"),
            details={"shared_workspace_only": True},
        )

    def request_advance(
        self, instance_id: str, action: str, payload: dict[str, object], operation_id: str
    ) -> AdapterCommandResult:
        del instance_id, action, payload, operation_id
        raise HarnessError(
            "INVALID_STATE_TRANSITION", "General Agent work is performed in its chat."
        )

    def collect_deliveries(self, instance_id: str) -> list[DeliveryCandidate]:
        del instance_id
        return []

    def collect_usage(self, instance_id: str, cursor: str | None) -> list[UsageEvent]:
        task_id = self._task_id_for_instance(instance_id)
        instance = self.store.instance.get(task_id, instance_id)
        process = None if instance is None else instance.get("process")
        if not isinstance(process, dict) or process.get("state") != "RUNNING":
            return []
        try:
            with urlopen(
                f"http://{self.host}:{int(process['port'])}/api/usage", timeout=3
            ) as response:
                payload = json.loads(response.read(8 * 1024 * 1024))
        except (OSError, ValueError) as exc:
            raise HarnessError(
                "USAGE_COLLECTION_FAILED", "General Agent usage could not be collected."
            ) from exc
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            raise HarnessError(
                "USAGE_COLLECTION_FAILED", "General Agent usage response is invalid."
            )
        start = 0
        if cursor is not None:
            matches = [index for index, item in enumerate(events) if item.get("event_id") == cursor]
            if matches:
                start = matches[-1] + 1
        for event in events[start:]:
            self.contracts.validate("token-usage-event-v1.1", event)
        return events[start:]

    def get_ui_url(self, instance_id: str) -> str | None:
        task_id = self._task_id_for_instance(instance_id)
        instance = self.store.instance.get(task_id, instance_id)
        return None if instance is None else instance.get("ui_url")

    def validate_ui_url(
        self, instance: AgentInstanceSnapshot, ui_url: str
    ) -> ValidationResult:
        try:
            parsed = urlsplit(ui_url)
            process = instance.get("process")
            valid = bool(
                parsed.scheme == "http"
                and parsed.hostname == self.host
                and parsed.path in {"", "/"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and isinstance(process, dict)
                and process.get("state") == "RUNNING"
                and parsed.port == process.get("port")
            )
        except ValueError:
            valid = False
        return ValidationResult(valid, () if valid else ("The General Agent UI URL is invalid.",))

    def recover(self, instance_snapshot: AgentInstanceSnapshot) -> AdapterRecoveryResult:
        task_id = str(instance_snapshot["task_id"])
        instance_id = str(instance_snapshot["instance_id"])
        if not self._state_path(task_id, instance_id).exists():
            return AdapterRecoveryResult(False, "FAILED")
        return AdapterRecoveryResult(True, str(instance_snapshot["status"]))

    def _task_id_for_instance(self, instance_id: str) -> str:
        validate_identifier(instance_id, "instance_id")
        root = self.store.layout.control_root / "tasks"
        matches = [
            path.parent.parent.name
            for path in root.glob(f"*/instances/{instance_id}.json")
            if path.is_file()
        ]
        if len(matches) != 1:
            raise HarnessError(
                "INSTANCE_NOT_FOUND", "The General Agent could not resolve one unique instance."
            )
        return matches[0]

    def _state_path(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.initialize_instance(task_id, instance_id)
            / "runtime"
            / "general-adapter.json"
        )

    @staticmethod
    def _migrate_legacy_chat_state(
        shared_root: Path, managed_state_root: Path, chat_state_path: Path
    ) -> None:
        """Move chat state kept inside the shared folder by older revisions.

        Revisions that stored ``resources/shared/.general-agent-state`` leaked
        internal bookkeeping into the user-facing delivery area. Adopting the
        existing file keeps in-flight chats intact while emptying the shared
        folder of non-deliverable files.
        """

        legacy_root = shared_root / ".general-agent-state"
        if legacy_root.is_symlink() or not legacy_root.is_dir():
            return
        legacy_state = legacy_root / chat_state_path.name
        if (
            not chat_state_path.exists()
            and legacy_state.is_file()
            and not legacy_state.is_symlink()
        ):
            try:
                shutil.move(str(legacy_state), str(chat_state_path))
            except OSError as exc:
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The legacy General Agent chat state could not be migrated.",
                ) from exc
        with contextlib.suppress(OSError):
            legacy_root.rmdir()

    def _prepare_runtime_artifact(self) -> Path:
        source_digest = content_tree_sha256(
            self.source_root, ignored_root_names={"__pycache__"}
        )
        cache_root = self.source_root.parent.parent / ".runtime" / "general-agent"
        destination = cache_root / f"1.0.0-{source_digest[:16]}"
        if destination.is_dir():
            if content_tree_sha256(destination) != source_digest:
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE", "The cached General Agent runtime is invalid."
                )
            return destination.resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".general-runtime-", dir=cache_root))
        try:
            shutil.copytree(
                self.source_root,
                temporary,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            if content_tree_sha256(temporary) != source_digest:
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE", "The copied General Agent runtime is invalid."
                )
            for path in sorted(temporary.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            temporary.chmod(0o555)
            try:
                temporary.replace(destination)
            except OSError:
                if not destination.is_dir():
                    raise
            if content_tree_sha256(destination) != source_digest:
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE", "The cached General Agent runtime is invalid."
                )
            return destination.resolve()
        finally:
            if temporary.exists():
                for path in temporary.rglob("*"):
                    path.chmod(0o755 if path.is_dir() else 0o644)
                temporary.chmod(0o755)
                shutil.rmtree(temporary)
