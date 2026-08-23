"""Image Agent observation, compatibility and HTTP protocol boundary."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.errors import HarnessError
from .base import AdapterObservation
from .image_workflow import (
    HARNESS_CAPABILITIES,
    KNOWN_CAPABILITIES,
    RUNNING_PHASES,
    WAITING_PHASES,
    approval_context,
)
from .image_workflow import (
    normalized_capabilities as normalize_workflow_capabilities,
)

SUPPORTED_IMAGE_API_MAJOR = "1"
MANAGED_ADAPTER_HEADER = "X-Harness-Adapter-Key"
_JOB_ID = re.compile(r"^job_[A-Za-z0-9]+$")
_JOB_STATES = frozenset(
    {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"}
)
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling"})
_FAILED_JOB_STATES = frozenset({"failed", "cancelled", "interrupted"})
_REQUIRED_ROUTES = frozenset(
    {
        "/api/health",
        "/api/jobs/{job_id}",
        "/api/managed/projects",
        "/api/projects",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/delivery/candidates/finalize",
        "/api/projects/{project_id}/delivery/finalize",
        "/api/projects/{project_id}/jobs",
        "/api/projects/{project_id}/timeline",
        "/api/projects/{project_id}/usage",
    }
)
_REQUIRED_ROUTE_METHODS = (
    ("/api/health", "get"),
    ("/api/jobs/{job_id}", "get"),
    ("/api/jobs/{job_id}/cancel", "post"),
    ("/api/managed/projects", "post"),
    ("/api/projects", "post"),
    ("/api/projects/{project_id}", "get"),
    ("/api/projects/{project_id}/delivery/candidates/finalize", "post"),
    ("/api/projects/{project_id}/delivery/finalize", "post"),
    ("/api/projects/{project_id}/jobs", "post"),
    ("/api/projects/{project_id}/timeline", "get"),
    ("/api/projects/{project_id}/usage", "get"),
)


def _raise_protocol_error(message: str) -> NoReturn:
    raise HarnessError("VALIDATION_ERROR", message)


class ImageObservationMixin:
    """Image Agent protocol, compatibility and observation behavior."""

    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

        @staticmethod
        def _protocol_error(message: str) -> NoReturn: ...

    def _observation(
        self,
        view: dict[str, Any],
        job: dict[str, Any] | None,
        timeline_cursor: int,
        compatibility: dict[str, Any] | None,
    ) -> AdapterObservation:
        job_status = None if job is None else job["status"]
        details: dict[str, Any] = {
            "job_id": None if job is None else job["job_id"],
            "job_status": job_status,
            "timeline_cursor": timeline_cursor,
            "compatibility": compatibility,
        }
        manifest = view.get("manifest")
        snapshot = view.get("snapshot")
        capabilities = view.get("capabilities")
        if (
            not isinstance(manifest, dict)
            or not isinstance(snapshot, dict)
            or not isinstance(capabilities, list)
        ):
            return self._compatibility_failure(details, "Image project response is malformed.")
        failed_step = manifest.get("failed_step")
        if failed_step is not None and not isinstance(failed_step, dict):
            return self._compatibility_failure(details, "Image project manifest is malformed.")
        if any(
            not isinstance(item, str) or item not in KNOWN_CAPABILITIES for item in capabilities
        ) or len(capabilities) != len(set(capabilities)):
            return self._compatibility_failure(
                details, "Image Agent returned an unknown capability or duplicate capability."
            )
        if snapshot and (
            ("waiting" in snapshot and type(snapshot["waiting"]) is not bool)
            or ("completed" in snapshot and type(snapshot["completed"]) is not bool)
        ):
            return self._compatibility_failure(
                details, "Image Agent returned a malformed snapshot."
            )
        phase = snapshot.get("phase")
        if snapshot and (
            not isinstance(phase, str) or phase not in WAITING_PHASES | RUNNING_PHASES
        ):
            return self._compatibility_failure(details, "Image Agent returned an unknown phase.")
        if not snapshot and capabilities:
            return self._compatibility_failure(
                details, "An empty Image snapshot published capabilities."
            )
        if snapshot:
            state_name = snapshot.get("state")
            if state_name is not None and not isinstance(state_name, str):
                return self._compatibility_failure(
                    details, "Image Agent returned a malformed workflow state."
                )
            normalized_capabilities = normalize_workflow_capabilities(snapshot, capabilities)
            normalized_capabilities = tuple(
                item for item in normalized_capabilities if item in HARNESS_CAPABILITIES
            )
            details.update(
                {
                    "phase": phase,
                    "state": state_name,
                    "completed": snapshot.get("completed", False),
                    "approval_context": approval_context(snapshot),
                }
            )
        else:
            normalized_capabilities = tuple(capabilities)
        if job_status == "succeeded" and not snapshot:
            return self._compatibility_failure(
                details,
                "A succeeded Image job did not publish a non-empty snapshot.",
            )
        if job_status in _ACTIVE_JOB_STATES:
            return AdapterObservation(status="RUNNING", details=details)
        if job is not None and job_status in _FAILED_JOB_STATES:
            details["error"] = deepcopy(job.get("error"))
            return AdapterObservation(status="FAILED", details=details)
        if failed_step:
            details["error"] = deepcopy(failed_step)
            return AdapterObservation(status="FAILED", details=details)
        if not snapshot:
            return AdapterObservation(status="RUNNING", details=details)
        if snapshot.get("completed") is True:
            return AdapterObservation(
                status="RUNNING",
                step_id=phase,
                capabilities=(),
                details=details,
            )
        if phase in WAITING_PHASES:
            if snapshot.get("waiting") is not True or not normalized_capabilities:
                return self._compatibility_failure(
                    details,
                    "A waiting Image phase did not publish its waiting flag and capability.",
                )
            return AdapterObservation(
                status="WAITING_APPROVAL",
                step_id=phase,
                capabilities=normalized_capabilities,
                details=details,
            )
        if snapshot.get("waiting") is True:
            return self._compatibility_failure(
                details, "A running Image phase unexpectedly published a waiting flag."
            )
        if phase in {"candidate_generation_completed", "calibration_completed"}:
            if not normalized_capabilities:
                return self._compatibility_failure(
                    details, "An Image decision phase did not publish a legal action."
                )
            return AdapterObservation(
                status="WAITING_APPROVAL",
                step_id=phase,
                capabilities=normalized_capabilities,
                details=details,
            )
        return AdapterObservation(
            status="RUNNING",
            step_id=phase,
            capabilities=normalized_capabilities,
            details=details,
        )

    @staticmethod
    def _compatibility_failure(details: dict[str, Any], message: str) -> AdapterObservation:
        return AdapterObservation(
            status="FAILED",
            details={**details, "compatibility_error": message},
        )

    def _check_compatibility(self, base_url: str) -> dict[str, Any]:
        document = self._request(base_url, "GET", "/openapi.json")
        if not isinstance(document, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        info = document.get("info")
        paths = document.get("paths")
        components_root = document.get("components")
        if (
            not isinstance(info, dict)
            or not isinstance(paths, dict)
            or not isinstance(components_root, dict)
        ):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        components = components_root.get("schemas")
        if not isinstance(components, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        api_version = info.get("version")
        create_schema = components.get("CreateProjectRequest")
        advance_schema = components.get("AdvanceRequest")
        required_routes_valid = all(
            isinstance(paths.get(route), dict) and isinstance(paths[route].get(method), dict)
            for route, method in _REQUIRED_ROUTE_METHODS
        )
        if not isinstance(create_schema, dict) or not isinstance(advance_schema, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        create_properties = create_schema.get("properties")
        advance_properties = advance_schema.get("properties")
        if not isinstance(create_properties, dict) or not isinstance(advance_properties, dict):
            self._protocol_error("Image Agent OpenAPI metadata is malformed.")
        if (
            not isinstance(api_version, str)
            or api_version.split(".", 1)[0] != SUPPORTED_IMAGE_API_MAJOR
            or not _REQUIRED_ROUTES.issubset(paths)
            or not required_routes_valid
            or "defer_run" not in create_properties
            or "idempotency_key" not in advance_properties
        ):
            self._protocol_error("Image Agent API version or capabilities are unsupported.")
        return {
            "api_version": api_version,
            "source_revision": self.revision,
            "package_version": self.package_version,
        }

    def _request(
        self,
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_statuses: tuple[int, ...] = (200,),
        allow_404: bool = False,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            request_headers.update(headers)
        request = Request(
            f"{base_url}{path}",
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urlopen(request, timeout=self.request_timeout_seconds) as response:
                if response.status not in expected_statuses:
                    self._protocol_error("Image Agent returned an unexpected HTTP status.")
                content_type = response.headers.get_content_type()
                raw = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            raise HarnessError(
                "PROCESS_START_FAILED",
                "Image Agent rejected a local adapter request.",
                {"http_status": exc.code, "path": path.split("?", 1)[0]},
            ) from None
        except (OSError, TimeoutError, URLError):
            raise HarnessError(
                "PROCESS_START_FAILED",
                "Image Agent did not answer a local adapter request.",
                {"path": path.split("?", 1)[0]},
            ) from None
        if len(raw) > 8 * 1024 * 1024 or content_type != "application/json":
            self._protocol_error("Image Agent returned an unsafe response body.")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._protocol_error("Image Agent returned invalid JSON.")
        if not isinstance(value, dict):
            self._protocol_error("Image Agent returned a non-object response.")
        return value

    @staticmethod
    def _require_job(job: dict[str, Any] | None) -> dict[str, Any]:
        if (
            not isinstance(job, dict)
            or not isinstance(job.get("job_id"), str)
            or _JOB_ID.fullmatch(job["job_id"]) is None
            or not isinstance(job.get("status"), str)
            or job["status"] not in _JOB_STATES
        ):
            _raise_protocol_error("Image Agent returned an invalid job record.")
        for field in (
            "project_id",
            "operation",
            "idempotency_key",
            "created_at",
            "started_at",
            "finished_at",
        ):
            if field in job and not isinstance(job[field], str):
                _raise_protocol_error(
                    "Image Agent returned an invalid job record."
                )
        error = job.get("error")
        if error is not None and (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
            or ("category" in error and not isinstance(error["category"], str))
        ):
            _raise_protocol_error("Image Agent returned an invalid job record.")
        if job["status"] in {"failed", "interrupted"} and error is None:
            _raise_protocol_error("Image Agent returned an invalid job record.")
        if job["status"] == "succeeded" and not isinstance(job.get("result"), dict):
            _raise_protocol_error("Image Agent returned an invalid job record.")
        if "cancellation_requested" in job and type(job["cancellation_requested"]) is not bool:
            _raise_protocol_error("Image Agent returned an invalid job record.")
        events = job.get("events")
        if events is not None:
            if not isinstance(events, list):
                _raise_protocol_error(
                    "Image Agent returned an invalid job record."
                )
            previous_sequence = 0
            for event in events:
                if (
                    not isinstance(event, dict)
                    or type(event.get("seq")) is not int
                    or event["seq"] <= previous_sequence
                    or not isinstance(event.get("type"), str)
                    or not isinstance(event.get("timestamp"), str)
                ):
                    _raise_protocol_error(
                        "Image Agent returned an invalid job record."
                    )
                previous_sequence = event["seq"]
        return job

    @staticmethod
    def _validate_timeline(timeline: dict[str, Any] | None, previous: int) -> int:
        if (
            not isinstance(timeline, dict)
            or not isinstance(timeline.get("items"), list)
            or type(timeline.get("next_cursor")) is not int
            or type(timeline.get("has_more")) is not bool
            or len(timeline.get("items", [])) > 100
        ):
            _raise_protocol_error(
                "Image Agent returned an invalid timeline page."
            )
        sequences: list[int] = []
        for item in timeline["items"]:
            if (
                not isinstance(item, dict)
                or type(item.get("sequence")) is not int
                or not isinstance(item.get("type"), str)
                or not isinstance(item.get("timestamp"), str)
            ):
                _raise_protocol_error(
                    "Image Agent returned an invalid timeline page."
                )
            sequences.append(item["sequence"])
        cursor = timeline["next_cursor"]
        if (
            any(sequence <= previous for sequence in sequences)
            or any(current <= prior for prior, current in pairwise(sequences))
            or cursor < previous
            or cursor != (sequences[-1] if sequences else previous)
            or (timeline["has_more"] and not sequences)
        ):
            _raise_protocol_error("Image Agent timeline cursor is invalid.")
        return cursor
