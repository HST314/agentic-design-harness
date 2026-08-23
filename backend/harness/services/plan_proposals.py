"""Canonical validation gate for every durable PlanProposal write."""

from __future__ import annotations

from typing import Any

from ..contracts import ContractRegistry
from ..domain.master import (
    AssetSourceIndex,
    validate_plan_proposal,
    validate_source_citations,
)
from .asset_understanding import AssetUnderstandingService
from .task_config import TaskConfigService


class PlanProposalValidationService:
    """Validate proposal semantics and citations against durable source facts."""

    def __init__(
        self,
        contracts: ContractRegistry,
        task_config: TaskConfigService,
        asset_understanding: AssetUnderstandingService,
    ) -> None:
        self.contracts = contracts
        self.task_config = task_config
        self.asset_understanding = asset_understanding

    def validate_new(
        self,
        task_id: str,
        proposal: dict[str, Any],
        *,
        expected_revision: int,
    ) -> None:
        """Validate one new pending proposal before any durable checkpoint."""

        validate_plan_proposal(
            self.contracts,
            proposal,
            task_id=task_id,
            expected_revision=expected_revision,
        )
        self.validate_sources(task_id, proposal, contract_validated=True)

    def validate_sources(
        self,
        task_id: str,
        proposal: dict[str, Any],
        *,
        contract_validated: bool = False,
    ) -> None:
        """Validate citations before new or status-only PlanProposal writes."""

        if not contract_validated:
            self.contracts.validate("plan-proposal", proposal)
        if not self.task_config.source_citations_required(task_id):
            return
        asset_ids = {
            source["asset_id"]
            for card in proposal["execution_cards"]
            for source in card["input_assets"]
        }
        source_indexes: dict[str, AssetSourceIndex] = {}
        for asset_id in asset_ids:
            document = self.asset_understanding.load_persisted(task_id, asset_id)
            source_indexes[asset_id] = AssetSourceIndex(
                page_count=document["page_count"],
                block_ids=frozenset(block["block_id"] for block in document["blocks"]),
            )
        validate_source_citations(proposal, source_indexes)
