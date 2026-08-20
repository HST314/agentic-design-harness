"""Frozen Image workflow phases and strict Harness advance mappings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..core.errors import HarnessError

WAITING_PHASES = frozenset(
    {
        "waiting_category_approval",
        "waiting_clarification",
        "waiting_clarification_review",
        "waiting_human_approval",
        "waiting_human_tune",
        "waiting_master_selection",
        "waiting_reinspection",
        "waiting_skill_approval",
        "waiting_taskbook_revision",
        "terminated_without_delivery",
    }
)
RUNNING_PHASES = frozenset(
    {
        "additional_rounds_approved",
        "calibration_completed",
        "candidate_generation_completed",
        "category_approved",
        "completed",
        "master_selected",
        "offline_rehearsal_completed",
        "ready_for_category_match",
        "ready_for_clarification",
        "ready_for_final_approval",
        "ready_for_quality_inspection",
        "ready_for_style_direction",
        "ready_for_taskbook",
        "ready_to_draft",
        "round_checkpointed",
        "skill_approved_pending_render",
        "task_approved",
    }
)
KNOWN_CAPABILITIES = frozenset(
    {
        "abandon",
        "adjust_clarification_budget",
        "answer_clarification",
        "answer_taskbook_revision",
        "apply_clarification_safe_defaults",
        "apply_taskbook_scope_boundaries",
        "approve_category_constraint",
        "approve_skill_invocations",
        "branch",
        "build_taskbook",
        "choose_master",
        "continue_clarification_after_budget_change",
        "edit_rework",
        "edit_taskbook",
        "enter_human_tune",
        "inspect",
        "open_final_approval",
        "prepare_style_direction",
        "regenerate_taskbook",
        "render_candidates",
        "resume_quality_inspection",
        "retry",
        "retry_category_constraint",
        "retry_skill_invocations",
        "review_calibration",
        "select_master",
        "start_category_match",
        "start_clarification",
        "start_quality_inspection",
        "submit_human_tune",
    }
)
EMPTY_ADVANCE_ACTIONS = frozenset(
    {
        "build_taskbook",
        "choose_master",
        "open_final_approval",
        "prepare_style_direction",
        "render_candidates",
        "resume_quality_inspection",
        "start_category_match",
        "start_clarification",
        "start_quality_inspection",
    }
)
HARNESS_CAPABILITIES = EMPTY_ADVANCE_ACTIONS | frozenset(
    {
        "answer_clarification",
        "answer_taskbook_revision",
        "apply_clarification_safe_defaults",
        "apply_taskbook_scope_boundaries",
        "approve_category_constraint",
        "approve_final",
        "approve_skill_invocations",
        "approve_taskbook",
        "continue_clarification_after_budget_change",
        "edit_taskbook",
        "regenerate_taskbook",
        "retry_category_constraint",
        "retry_skill_invocations",
        "review_calibration",
        "select_master",
        "submit_human_tune",
    }
)


def normalized_capabilities(
    snapshot: dict[str, Any], capabilities: list[str]
) -> tuple[str, ...]:
    phase = snapshot.get("phase")
    state = snapshot.get("state")
    if phase == "waiting_human_approval" and state == "confirmation_build":
        return ("approve_taskbook",)
    if phase == "candidate_generation_completed":
        return ("select_master",)
    if phase == "calibration_completed" and snapshot.get("termination_satisfied") is True:
        return ("approve_final",)
    if state == "final_approval" and snapshot.get("completed") is not True:
        return ("approve_final",)
    return tuple(capabilities)


def approval_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    question_card = snapshot.get("question_card")
    if isinstance(question_card, dict):
        context["question_card"] = deepcopy(question_card)
    candidates = snapshot.get("candidates")
    if isinstance(candidates, list):
        context["candidates"] = [
            {
                key: deepcopy(item[key])
                for key in ("id", "candidate_id", "artifact_id", "sha256")
                if isinstance(item, dict) and key in item
            }
            for item in candidates
            if isinstance(item, dict)
        ]
    for key in ("inspection", "available_actions", "termination_reason"):
        if key in snapshot:
            context[key] = deepcopy(snapshot[key])
    return context


def map_advance_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HarnessError("VALIDATION_ERROR", "The Image action payload must be an object.")
    actor = payload.get("actor")
    if actor is not None and (not isinstance(actor, str) or not actor.strip()):
        raise HarnessError("VALIDATION_ERROR", "The Image action actor is invalid.")
    if action in EMPTY_ADVANCE_ACTIONS:
        allowed = {"actor"}
        result: dict[str, Any] = {}
    elif action == "approve_taskbook":
        allowed = {"actor"}
        result = {"task_approved": True}
    elif action == "approve_final":
        allowed = {"actor"}
        result = {"final_approved": True}
    elif action in {"approve_category_constraint", "retry_category_constraint"}:
        allowed = {"actor"}
        result = {
            "category_action": (
                "approve" if action == "approve_category_constraint" else "retry"
            )
        }
    elif action in {"approve_skill_invocations", "retry_skill_invocations"}:
        allowed = {"actor"}
        result = {
            "skill_action": "approve" if action == "approve_skill_invocations" else "retry"
        }
    elif action in {"answer_clarification", "answer_taskbook_revision"}:
        allowed = {"actor", "clarification_answers"}
        answers = payload.get("clarification_answers")
        if not isinstance(answers, dict):
            raise HarnessError(
                "VALIDATION_ERROR", "This Image action requires clarification_answers."
            )
        result = {"clarification_answers": deepcopy(answers)}
    elif action == "apply_clarification_safe_defaults":
        allowed = {"actor"}
        result = {"clarification_action": "apply_safe_defaults"}
    elif action == "continue_clarification_after_budget_change":
        allowed = {"actor"}
        result = {"clarification_action": "continue_after_budget_change"}
    elif action == "apply_taskbook_scope_boundaries":
        allowed = {"actor"}
        result = {"taskbook_action": "apply_scope_boundaries"}
    elif action == "regenerate_taskbook":
        allowed = {"actor"}
        result = {"taskbook_action": "regenerate"}
    elif action == "edit_taskbook":
        allowed = {"actor", "edited_markdown"}
        markdown = payload.get("edited_markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise HarnessError(
                "VALIDATION_ERROR", "This Image action requires edited_markdown."
            )
        result = {"edited_markdown": markdown}
    elif action == "select_master":
        allowed = {"actor", "selected_id"}
        selected_id = payload.get("selected_id")
        if not isinstance(selected_id, str) or not selected_id:
            raise HarnessError("VALIDATION_ERROR", "This Image action requires selected_id.")
        result = {"selected_id": selected_id}
    elif action == "review_calibration":
        allowed = {"actor", "manual_action", "edited_delta"}
        manual_action = payload.get("manual_action")
        if manual_action not in {
            "execute",
            "edit_and_execute",
            "skip",
            "end",
            "accept_current",
        }:
            raise HarnessError(
                "VALIDATION_ERROR", "This Image action requires a legal manual_action."
            )
        result = {"manual_action": manual_action}
        if manual_action == "accept_current":
            # The pinned Image workflow continues from a human quality
            # acceptance directly into final approval in the same async job.
            result["final_approved"] = True
        if manual_action == "edit_and_execute":
            edited_delta = payload.get("edited_delta")
            if not isinstance(edited_delta, str) or not edited_delta.strip():
                raise HarnessError(
                    "VALIDATION_ERROR", "edit_and_execute requires edited_delta."
                )
            result["edited_delta"] = edited_delta
    elif action == "submit_human_tune":
        allowed = {"actor", "human_prompt"}
        human_prompt = payload.get("human_prompt")
        if not isinstance(human_prompt, str) or not human_prompt.strip():
            raise HarnessError("VALIDATION_ERROR", "This Image action requires human_prompt.")
        result = {"human_prompt": human_prompt.strip()}
    else:
        raise HarnessError(
            "ADAPTER_UNAVAILABLE",
            "This Image capability must be completed in the Image workbench.",
            {"capability": action},
        )
    unexpected = set(payload) - allowed
    if unexpected:
        raise HarnessError(
            "VALIDATION_ERROR",
            "The Image action payload contains unsupported fields.",
            {"fields": sorted(unexpected)},
        )
    if actor is not None:
        result["actor"] = actor.strip()
    return result
