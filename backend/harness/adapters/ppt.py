"""Runnable PPT Agent adapter over its loopback HTTP workbench."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from ..services.process_runtime import (
    AgentRuntimeArtifact,
    AgentRuntimeIdentity,
    ProcessSpec,
    runtime_artifact_identity,
)
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.store import FileStateStore
from .base import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    PrepareRequest,
    ValidationResult,
)
from .image_runtime import content_tree_sha256, dependency_tree_sha256
from .ppt_attestation import attest_ppt_runtime
from .ppt_lock import PptAgentReleaseLock
from .types import AgentInstanceSnapshot, DeliveryCandidate, TaskCard, UsageEvent

_PACKAGE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_LAUNCHER = (
    "import runpy,sys; p=sys.argv[1]; "
    "sys.path.insert(0,str(__import__('pathlib').Path(p).parent)); "
    "m=runpy.run_path(p); "
    "__import__('uvicorn').run(m['app'],host=sys.argv[2],port=int(sys.argv[3]))"
)


class PptAgentAdapter:
    agent_type = "ppt"
    available = True

    def __init__(
        self,
        store: FileStateStore,
        contracts: ContractRegistry,
        *,
        source_root: Path,
        interpreter: Path,
        dependency_root: Path,
        release_lock: PptAgentReleaseLock,
        runtime_policy: Path,
        model_config: Path,
        host: str = "127.0.0.1",
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.contracts = contracts
        self.source_root = source_root
        self.interpreter = interpreter
        self.dependency_root = dependency_root
        self.release_lock = release_lock
        self.runtime_policy = runtime_policy
        self.model_config = model_config
        self.host = host
        self.request_timeout_seconds = request_timeout_seconds
        self._validate_runtime()
        self.runtime_root = self._prepare_runtime_artifact()
        self.runtime_identity = self._verify_runtime_identity()

    def validate_task_card(self, card: TaskCard) -> ValidationResult:
        try:
            self.contracts.validate("task-card", card)
        except (HarnessError, ValueError) as exc:
            return ValidationResult(False, (str(exc),))
        errors: list[str] = []
        if card.get("agent_type") != "ppt":
            errors.append("Task card agent_type must be ppt.")
        count = card.get("parameters", {}).get("slide_count")
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 500
        ):
            errors.append("PPT slide_count must be an integer between 1 and 500.")
        input_source = card.get("parameters", {}).get("input_source", "shared")
        if input_source not in {"shared", "empty"}:
            errors.append("PPT input_source must be shared or empty.")
        required = [item for item in card.get("expected_deliveries", []) if item.get("required")]
        if not any(item.get("kind") in {"presentation", "archive"} for item in required):
            errors.append("PPT Agent requires a presentation or archive delivery.")
        return ValidationResult(not errors, tuple(errors))

    def prepare(self, request: PrepareRequest) -> ProcessSpec:
        validation = self.validate_task_card(request.task_card)
        if not validation.valid:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The PPT Agent adapter rejected its task card.",
                {"errors": list(validation.errors)},
            )
        instance_id = str(request.instance["instance_id"])
        task_id = str(request.instance["task_id"])
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        runtime_root = instance_root / "runtime"
        projects_root = instance_root / "work" / "projects"
        input_source = str(request.task_card.get("parameters", {}).get("input_source", "shared"))
        source_images_root = (
            request.task_root / "resources" / "shared"
            if input_source == "shared"
            else instance_root / "work" / "empty-input"
        )
        projects_root.mkdir(parents=True, exist_ok=True)
        source_images_root.mkdir(parents=True, exist_ok=True)
        images_root, input_sha256 = self._prepare_read_only_input(
            source_images_root, instance_root / "work" / "input-snapshot"
        )
        read_only_mirrors = (
            ((source_images_root.resolve(), images_root),)
            if input_source == "shared"
            else ()
        )
        mapped = self.map_task_card(request)
        card_path = runtime_root / "ppt-task-card.json"
        atomic_write_json(card_path, mapped, mode=0o640)
        state_path = self._state_path(task_id, instance_id)
        expected = {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "task_card_sha256": digest_json(mapped),
            "source_revision": self.release_lock.revision,
            "input_source": input_source,
            "input_sha256": input_sha256,
            "project_created": False,
            "operation_id": None,
        }
        current = read_json(state_path) if state_path.exists() else None
        if current is not None and any(
            current.get(key) != expected[key]
            for key in (
                "task_id",
                "instance_id",
                "task_card_sha256",
                "source_revision",
                "input_source",
            )
        ):
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                "The prepared PPT runtime no longer matches its task card.",
                {"instance_id": instance_id},
            )
        state = expected if current is None else {**current, "input_sha256": input_sha256}
        atomic_write_json(state_path, state)
        artifact = self._runtime_artifact()
        entrypoint = self.runtime_root / "main_front.py"
        pythonpath = os.pathsep.join(
            (str(self.runtime_root), str(self.runtime_root / "_dependencies"))
        )
        return ProcessSpec(
            command=(str(self.interpreter), "-c", _LAUNCHER, str(entrypoint), "{host}", "{port}"),
            runtime_artifact=artifact,
            verified_runtime_identity=self.runtime_identity,
            public_environment={
                "PPT_AGENT_IMAGES_ROOT": str(images_root),
                "PPT_AGENT_PROJECTS_ROOT": str(projects_root),
                "PPT_AGENT_MODEL_CONFIG": str(self.model_config),
                "PPT_AGENT_RUNTIME_POLICY": str(self.runtime_policy),
                "PYTHONPATH": pythonpath,
            },
            writable_roots=(projects_root,),
            read_only_mirrors=read_only_mirrors,
            health_path="/api/health",
            readiness_path="/api/health",
            ui_path="/",
        )

    def map_task_card(self, request: PrepareRequest) -> dict[str, Any]:
        card = request.task_card
        count = card.get("parameters", {}).get("slide_count")
        instructions = [str(item) for item in card.get("instructions", [])]
        return {
            "title": str(card["objective"])[:160],
            "objective": str(card["objective"])[:4000],
            "target_slide_count": "" if count is None else str(count),
            "known_facts": [],
            "constraints": instructions,
            "source_refs": [str(item["asset_id"]) for item in card.get("input_assets", [])],
        }

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult:
        validate_identifier(operation_id, "operation_id")
        task_id = self._task_id_for_instance(instance_id)
        state = self._state(task_id, instance_id)
        base_url = self._base_url(task_id, instance_id)
        if not state.get("project_created"):
            existing = self._request(
                base_url, "GET", f"/api/projects/{instance_id}", allow_404=True
            )
            if existing is None:
                card = read_json(
                    self.store.layout.initialize_instance(task_id, instance_id)
                    / "runtime"
                    / "ppt-task-card.json"
                )
                self._request(
                    base_url,
                    "POST",
                    "/api/projects",
                    {"project_id": instance_id, "task_card": card},
                    expected_statuses=(201,),
                )
            state["project_created"] = True
        state["operation_id"] = operation_id
        self._write_state(task_id, instance_id, state)
        return AdapterCommandResult(
            True, operation_id, {"project_id": instance_id, "ui_managed": False}
        )

    def stop(self, instance_id: str, reason: str, operation_id: str) -> AdapterCommandResult:
        validate_identifier(operation_id, "operation_id")
        if not reason or len(reason) > 256 or "\x00" in reason:
            raise HarnessError("VALIDATION_ERROR", "The stop reason is invalid.")
        return AdapterCommandResult(True, operation_id, {"project_id": instance_id})

    def get_status(self, instance_id: str) -> AdapterObservation:
        task_id = self._task_id_for_instance(instance_id)
        view = self._request(
            self._base_url(task_id, instance_id), "GET", f"/api/projects/{instance_id}"
        )
        if not isinstance(view, dict):
            self._protocol_error("PPT Agent returned an invalid project view.")
        state, phase = view.get("state"), view.get("phase")
        caps = tuple(item for item in view.get("capabilities", []) if isinstance(item, str))
        return AdapterObservation(
            "RUNNING",
            step_id=f"{state}:{phase}",
            capabilities=caps,
            details={
                "project_id": instance_id,
                "state": state,
                "phase": phase,
                "completed": False,
                "export_ready": state == "acceptance",
                "workbench_managed": False,
            },
        )

    def request_advance(
        self, instance_id: str, action: str, payload: dict[str, Any], operation_id: str
    ) -> AdapterCommandResult:
        raise HarnessError(
            "INVALID_STATE_TRANSITION",
            "PPT workflow actions are performed in its workbench.",
            {"instance_id": instance_id, "action": action},
        )

    def collect_deliveries(self, instance_id: str) -> list[DeliveryCandidate]:
        return []

    def collect_usage(self, instance_id: str, cursor: str | None) -> list[UsageEvent]:
        return []

    def get_ui_url(self, instance_id: str) -> str | None:
        task_id = self._task_id_for_instance(instance_id)
        instance = self.store.instance.get(task_id, instance_id)
        return None if instance is None else instance.get("ui_url")

    def validate_ui_url(self, instance: AgentInstanceSnapshot, ui_url: str) -> ValidationResult:
        errors: list[str] = []
        try:
            parsed = urlsplit(ui_url)
            port = parsed.port
        except ValueError:
            return ValidationResult(False, ("The PPT workbench URL is malformed.",))
        process = instance.get("process")
        if parsed.scheme != "http" or parsed.hostname != self.host:
            errors.append("The PPT workbench must use its allocated local HTTP origin.")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            errors.append("The PPT workbench URL contains unsupported components.")
        if (
            not isinstance(process, dict)
            or process.get("state") != "RUNNING"
            or port != process.get("port")
        ):
            errors.append("The PPT workbench process allocation does not match the URL.")
        return ValidationResult(not errors, tuple(errors))

    def recover(self, instance_snapshot: AgentInstanceSnapshot) -> AdapterRecoveryResult:
        instance_id = str(instance_snapshot["instance_id"])
        task_id = str(instance_snapshot["task_id"])
        if not self._state_path(task_id, instance_id).exists():
            return AdapterRecoveryResult(False, "FAILED")
        if instance_snapshot["status"] not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
            return AdapterRecoveryResult(True, str(instance_snapshot["status"]))
        observation = self.get_status(instance_id)
        return AdapterRecoveryResult(True, observation.status, {"step_id": observation.step_id})

    def _validate_runtime(self) -> None:
        if (
            not self.source_root.is_absolute()
            or not self.source_root.is_dir()
            or self.source_root.is_symlink()
            or not self.interpreter.is_absolute()
            or not self.interpreter.is_file()
        ):
            raise HarnessError(
                "ADAPTER_UNAVAILABLE", "The PPT Agent source or interpreter is not configured."
            )
        for path in (self.runtime_policy, self.model_config):
            if not path.is_absolute() or not path.is_file():
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE", "A managed PPT Agent configuration file is missing."
                )
        try:
            match = _PACKAGE_VERSION.search(
                (self.source_root / "pyproject.toml").read_text(encoding="utf-8")
            )
        except OSError:
            match = None
        if match is None or match.group(1) != self.release_lock.package_version:
            raise HarnessError(
                "SCHEMA_VERSION_UNSUPPORTED",
                "The configured PPT Agent package version is unsupported.",
            )
        if content_tree_sha256(self.source_root) != self.release_lock.source_content_sha256:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE", "The PPT Agent source does not match its release lock."
            )
        self.dependency_sha256 = attest_ppt_runtime(
            self.release_lock,
            source_root=self.source_root,
            dependency_root=self.dependency_root,
            harness_root=self.source_root.parent.parent,
            interpreter=self.interpreter,
        )

    def _prepare_read_only_input(self, source: Path, destination: Path) -> tuple[Path, str]:
        """Publish the initial private mirror; the child never receives the shared path."""

        source_sha256 = content_tree_sha256(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".ppt-input-", dir=destination.parent)
        )
        try:
            shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=True)
            if content_tree_sha256(source) != source_sha256:
                raise HarnessError(
                    "PROCESS_START_FAILED", "The shared input changed while it was snapshotted."
                )
            if content_tree_sha256(temporary) != source_sha256:
                raise HarnessError(
                    "PROCESS_START_FAILED", "The PPT input snapshot does not match shared assets."
                )
            for path in sorted(temporary.rglob("*"), reverse=True):
                path.chmod(0o700 if path.is_dir() else 0o600)
            temporary.chmod(0o700)
            if destination.exists():
                self._make_removable(destination)
                shutil.rmtree(destination)
            temporary.replace(destination)
            return destination.resolve(), source_sha256
        finally:
            if temporary.exists():
                self._make_removable(temporary)
                shutil.rmtree(temporary)

    @staticmethod
    def _make_removable(root: Path) -> None:
        with suppress(OSError):
            root.chmod(0o700)
        for path in root.rglob("*"):
            with suppress(OSError):
                path.chmod(0o700 if path.is_dir() else 0o600)

    def _runtime_artifact(self) -> AgentRuntimeArtifact:
        return AgentRuntimeArtifact(
            "ppt-agent",
            self.release_lock.revision,
            self.runtime_root,
            "main_front.py",
            ("pyproject.toml", "_harness-ppt-requirements.lock"),
            self.interpreter.parent.parent,
        )

    def _verify_runtime_identity(self) -> AgentRuntimeIdentity:
        entrypoint = self.runtime_root / "main_front.py"
        return runtime_artifact_identity(
            ProcessSpec(
                command=(
                    str(self.interpreter),
                    "-c",
                    _LAUNCHER,
                    str(entrypoint),
                    "{host}",
                    "{port}",
                ),
                runtime_artifact=self._runtime_artifact(),
            )
        )

    def _prepare_runtime_artifact(self) -> Path:
        cache_root = self.dependency_root.parent / "ppt-runtime"
        destination = cache_root / f"{self.release_lock.revision}-{self.dependency_sha256[:16]}"
        if destination.is_dir():
            return self._validated_cached_runtime(destination)
        cache_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".ppt-runtime-", dir=cache_root))
        try:
            shutil.copytree(
                self.source_root,
                temporary,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", ".pytest_cache", ".ruff_cache"
                ),
            )
            if content_tree_sha256(temporary) != self.release_lock.source_content_sha256:
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE", "The copied PPT Agent source does not match its lock."
                )
            shutil.copytree(self.dependency_root, temporary / "_dependencies")
            shutil.copyfile(
                self.source_root.parent.parent / "requirements" / "ppt-agent.lock",
                temporary / "_harness-ppt-requirements.lock",
            )
            for path in sorted(temporary.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            temporary.chmod(0o555)
            try:
                temporary.replace(destination)
            except OSError:
                if not destination.is_dir():
                    raise
            return self._validated_cached_runtime(destination)
        finally:
            if temporary.exists():
                temporary.chmod(0o755)
                for path in temporary.rglob("*"):
                    with suppress(OSError):
                        path.chmod(0o755 if path.is_dir() else 0o644)
                shutil.rmtree(temporary)

    def _validated_cached_runtime(self, destination: Path) -> Path:
        if content_tree_sha256(
            destination,
            ignored_names={"_harness-ppt-requirements.lock"},
            ignored_root_names={"_dependencies"},
        ) != self.release_lock.source_content_sha256:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The cached PPT Agent runtime does not match its release lock.",
            )
        if dependency_tree_sha256(destination / "_dependencies") != self.dependency_sha256:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The cached PPT Agent dependencies do not match their proof.",
            )
        return destination.resolve()

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
                "INSTANCE_NOT_FOUND",
                "The PPT adapter could not resolve one unique instance.",
                {"instance_id": instance_id},
            )
        return matches[0]

    def _base_url(self, task_id: str, instance_id: str) -> str:
        instance = self.store.instance.get(task_id, instance_id)
        process = None if instance is None else instance.get("process")
        if not isinstance(process, dict) or process.get("state") != "RUNNING":
            raise HarnessError("PROCESS_START_FAILED", "The PPT Agent process is not running.")
        return f"http://{self.host}:{int(process['port'])}"

    def _state_path(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.initialize_instance(task_id, instance_id)
            / "runtime"
            / "ppt-adapter.json"
        )

    def _state(self, task_id: str, instance_id: str) -> dict[str, Any]:
        path = self._state_path(task_id, instance_id)
        if not path.exists():
            raise HarnessError("PROCESS_START_FAILED", "The PPT Agent adapter was not prepared.")
        return read_json(path)

    def _write_state(self, task_id: str, instance_id: str, state: dict[str, Any]) -> None:
        atomic_write_json(self._state_path(task_id, instance_id), state)

    def _request(
        self,
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_statuses: tuple[int, ...] = (200,),
        allow_404: bool = False,
    ) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            base_url + path,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.request_timeout_seconds) as response:
                if response.status not in expected_statuses:
                    self._protocol_error("PPT Agent returned an unexpected HTTP status.")
                content = response.read()
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            raise HarnessError(
                "PROCESS_START_FAILED",
                "PPT Agent rejected the adapter request.",
                {"http_status": exc.code},
            ) from None
        except (OSError, TimeoutError, URLError):
            raise HarnessError(
                "PROCESS_START_FAILED", "PPT Agent did not respond on its loopback endpoint."
            ) from None
        try:
            return json.loads(content) if content else None
        except json.JSONDecodeError:
            self._protocol_error("PPT Agent returned invalid JSON.")

    @staticmethod
    def _protocol_error(message: str) -> NoReturn:
        raise HarnessError("VALIDATION_ERROR", message)
