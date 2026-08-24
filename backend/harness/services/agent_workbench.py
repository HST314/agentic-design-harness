"""Trusted Image Agent workbench links and frame-policy inspection."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..adapters.registry import AdapterRegistry
from ..adapters.types import AgentInstanceSnapshot
from ..core.errors import HarnessError
from ..storage.layout import validate_identifier
from ..storage.store import FileStateStore
from .application import HarnessApplicationService
from .work_item_projections import WorkItemProjectionService


@dataclass(frozen=True, slots=True)
class FramePolicyResult:
    embeddable: bool
    policy: str
    diagnostic: str


class _RejectRedirects(HTTPRedirectHandler):
    """A UI probe must not leave the Adapter-approved origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), port


def _source_allows_origin(source: str, harness_origin: str, ui_origin: str) -> bool:
    normalized = source.strip()
    if normalized == "*":
        return True
    if normalized.lower() == "'self'":
        return _origin(harness_origin) == _origin(ui_origin)
    if normalized.lower() in {"'none'", "none"}:
        return False
    return _origin(normalized) == _origin(harness_origin)


def _csp_allows_embedding(
    policies: list[str], harness_origin: str, ui_origin: str
) -> bool:
    for policy in policies:
        frame_sources: list[str] | None = None
        for raw_directive in policy.split(";"):
            parts = raw_directive.strip().split()
            if parts and parts[0].lower() == "frame-ancestors":
                frame_sources = parts[1:]
                break
        if frame_sources is not None and not any(
            _source_allows_origin(source, harness_origin, ui_origin)
            for source in frame_sources
        ):
            return False
    return True


def inspect_frame_policy(
    ui_url: str,
    harness_origin: str,
    *,
    timeout_seconds: float = 2.0,
) -> FramePolicyResult:
    """Read only approved local UI headers; redirects are deliberately rejected."""

    request = Request(
        ui_url,
        method="GET",
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Harness-Workbench-Probe/1.0",
        },
    )
    try:
        with build_opener(_RejectRedirects).open(
            request, timeout=timeout_seconds
        ) as response:
            headers: Message = response.headers
            response_url = response.geturl()
            if _origin(response_url) != _origin(ui_url):
                return FramePolicyResult(
                    False,
                    "REDIRECT_REJECTED",
                    "Image Agent redirected outside its approved workbench URL.",
                )
            content_type = headers.get_content_type().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                return FramePolicyResult(
                    False,
                    "CONTENT_TYPE_REJECTED",
                    "Image Agent workbench did not return an HTML document.",
                )
            x_frame_options = (headers.get("X-Frame-Options") or "").strip().upper()
            x_frame_tokens = {
                value.strip().split(";", maxsplit=1)[0]
                for value in x_frame_options.split(",")
                if value.strip()
            }
            if "DENY" in x_frame_tokens:
                return FramePolicyResult(
                    False,
                    "X_FRAME_OPTIONS_BLOCKED",
                    "Image Agent returned X-Frame-Options: DENY.",
                )
            if "SAMEORIGIN" in x_frame_tokens and _origin(harness_origin) != _origin(
                ui_url
            ):
                return FramePolicyResult(
                    False,
                    "X_FRAME_OPTIONS_BLOCKED",
                    "Image Agent only permits same-origin framing.",
                )
            policies = headers.get_all("Content-Security-Policy", [])
            if policies and not _csp_allows_embedding(policies, harness_origin, ui_url):
                return FramePolicyResult(
                    False,
                    "FRAME_ANCESTORS_BLOCKED",
                    "Image Agent frame-ancestors does not permit this Harness origin.",
                )
            return FramePolicyResult(
                True,
                "FRAME_ANCESTORS_ALLOWED" if policies else "FRAME_ANCESTORS_NOT_DECLARED",
                (
                    "Image Agent explicitly permits this Harness origin."
                    if policies
                    else "Image Agent does not declare a frame-ancestors restriction."
                ),
            )
    except HTTPError as exc:
        return FramePolicyResult(
            False,
            "WORKBENCH_UNREACHABLE",
            f"Image Agent workbench probe returned HTTP {exc.code}.",
        )
    except (OSError, TimeoutError, URLError, ValueError):
        return FramePolicyResult(
            False,
            "WORKBENCH_UNREACHABLE",
            "Image Agent workbench did not respond to the frame-policy probe.",
        )


class AgentWorkbenchService:
    """Resolve a WorkItem's current Image instance without trusting browser URLs."""

    def __init__(
        self,
        store: FileStateStore,
        adapters: AdapterRegistry,
        work_items: WorkItemProjectionService,
        application: HarnessApplicationService,
        *,
        probe_timeout_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.work_items = work_items
        self.application = application
        self.probe_timeout_seconds = probe_timeout_seconds

    def get_link(
        self,
        task_id: str,
        work_item_id: str,
        instance_id: str,
        harness_origin: str,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(work_item_id, "work_item_id")
        validate_identifier(instance_id, "instance_id")
        detail = self.work_items.get(task_id, work_item_id)
        item = detail["item"]
        current = item.get("current_instance")
        if (
            not isinstance(current, dict)
            or current.get("instance_id") != instance_id
            or instance_id not in item["instance_ids"]
        ):
            raise HarnessError(
                "INSTANCE_NOT_FOUND",
                "The requested instance is not the current instance of this WorkItem.",
            )
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance.get("task_id") != task_id:
            raise HarnessError(
                "INSTANCE_NOT_FOUND",
                "The requested instance does not belong to this task.",
            )
        adapter = self.adapters.get(instance["agent_type"])
        operation = self.application.latest_start_operation(
            task_id, instance_id=instance_id
        )
        start_failure = instance.get("start_failure")
        if operation is not None and operation["state"] in {"QUEUED", "RUNNING"}:
            initial_status = "STARTING"
            diagnostic = "The Image Agent start operation is still running."
        elif isinstance(start_failure, dict):
            initial_status = "START_FAILED"
            diagnostic = str(start_failure["message"])
        else:
            initial_status = "NO_UI_URL"
            diagnostic = "The current instance has not published a workbench URL."
        response: dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": task_id,
            "work_item_id": work_item_id,
            "instance_id": instance_id,
            "agent_type": instance["agent_type"],
            "instance_status": instance["status"],
            "task_revision": self.store.task.revision(task_id, task_id),
            "ui_url": None,
            "link_status": "ADAPTER_UNAVAILABLE" if not adapter.available else initial_status,
            "start_operation": operation,
            "embeddable": False,
            "frame_policy": "NOT_CHECKED",
            "diagnostic": (
                f"{instance['agent_type'].upper()} capability is not available."
                if not adapter.available
                else diagnostic
            ),
        }
        if not adapter.available or initial_status in {"STARTING", "START_FAILED"}:
            return response
        ui_url = adapter.get_ui_url(instance_id)
        if ui_url is None:
            return response
        validation = adapter.validate_ui_url(
            cast(AgentInstanceSnapshot, instance), ui_url
        )
        if not validation.valid:
            raise HarnessError(
                "UI_LINK_REJECTED",
                "The Agent workbench URL failed its Adapter allowlist.",
                {"instance_id": instance_id, "errors": list(validation.errors)},
            )
        if _origin(ui_url) == _origin(harness_origin):
            raise HarnessError(
                "UI_LINK_REJECTED",
                "The Agent workbench must remain cross-origin when sandboxed scripts are enabled.",
                {"instance_id": instance_id},
            )
        frame = inspect_frame_policy(
            ui_url,
            harness_origin,
            timeout_seconds=self.probe_timeout_seconds,
        )
        response.update(
            {
                "ui_url": ui_url,
                "link_status": "READY" if frame.embeddable else "FRAME_BLOCKED",
                "embeddable": frame.embeddable,
                "frame_policy": frame.policy,
                "diagnostic": frame.diagnostic,
            }
        )
        return response
