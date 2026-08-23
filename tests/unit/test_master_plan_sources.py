from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from harness.domain.master import AssetSourceIndex, validate_source_citations

ROOT = Path(__file__).resolve().parents[2]


class MasterPlanSourceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proposal = json.loads(
            (ROOT / "contracts/v1/examples/objects/plan-proposal.json").read_text(
                encoding="utf-8"
            )
        )
        self.source_indexes = {
            "asset_brand_guide": AssetSourceIndex(
                page_count=1,
                block_ids=frozenset({"brand_block"}),
            )
        }

    def test_rejects_a_nonexistent_persisted_block(self) -> None:
        self.proposal["execution_cards"][0]["instructions"] = [
            "引用 asset_brand_guide/block/does_not_exist。"
        ]

        with self.assertRaisesRegex(HarnessError, "block that does not exist"):
            validate_source_citations(self.proposal, self.source_indexes)

    def test_rejects_a_page_beyond_the_persisted_page_count(self) -> None:
        self.proposal["execution_cards"][0]["instructions"] = [
            "引用 asset_brand_guide/page/2。"
        ]

        with self.assertRaisesRegex(HarnessError, "page that does not exist"):
            validate_source_citations(self.proposal, self.source_indexes)

    def test_accepts_existing_page_and_block_boundaries(self) -> None:
        self.proposal["execution_cards"][0]["instructions"] = [
            "引用 asset_brand_guide/page/1 与 asset_brand_guide/block/brand_block。"
        ]

        validate_source_citations(self.proposal, self.source_indexes)


if __name__ == "__main__":
    unittest.main()
