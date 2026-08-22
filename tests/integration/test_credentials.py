from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import yaml
from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.credentials import CredentialPoolService
from harness.storage.ndjson import recover_records
from harness.storage.repository import Actor
from runtime_helpers import build_service, create_task

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "p1" / "credential-pairs.json"


def initial_instance(task_id: str, instance_id: str, agent_type: str = "image") -> dict:
    return {
        "schema_version": "1.0",
        "instance_id": instance_id,
        "task_id": task_id,
        "stage_id": f"s_{instance_id}",
        "agent_type": agent_type,
        "required": True,
        "requirement_lifecycle": {
            "original_required": True,
            "first_activated_at": None,
            "authorized_downgrade": None,
        },
        "status": "CREATED",
        "approval_mode": "human",
        "config_revision": 1,
        "workspace_relpath": f"instances/{instance_id}",
        "task_card_relpath": f"instances/{instance_id}/task-card.json",
        "ui_url": None,
        "process": None,
        "created_at": "2026-08-20T12:00:00Z",
    }


class CredentialPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "t_credentials")
        self.pool = CredentialPoolService(self.store)
        self.pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
        self.pool.configure_pool(self.pairs)
        self.actor = Actor("human", "tester")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _create(self, index: int) -> dict:
        instance_id = f"i_credential_{index}"
        return self.pool.create_instance(
            "t_credentials",
            initial_instance("t_credentials", instance_id),
            provider="fake",
            creation_id=f"creation_{index}",
            actor=self.actor,
        )

    def test_commit_order_round_robin_is_complete_sticky_and_redacted(self) -> None:
        results = [self._create(index) for index in range(1, 5)]
        self.assertEqual(
            [item["credential"]["credential_pair_id"] for item in results],
            ["cred_test_01", "cred_test_02", "cred_test_03", "cred_test_01"],
        )
        for index, result in enumerate(results, start=1):
            resolved = self.pool.resolve_for_instance(
                "t_credentials", f"i_credential_{index}"
            )
            expected = self.pairs[(index - 1) % 3]
            self.assertEqual(resolved.credential_pair_id, expected["credential_pair_id"])
            self.assertEqual(resolved.key_id, expected["key_id"])
            self.assertEqual(resolved.base_url, expected["base_url"])
            self.assertEqual(resolved.revision, expected["revision"])
            self.assertEqual(
                resolved.as_environment(),
                {
                    expected["api_key_env"]: expected["api_key"],
                    expected["base_url_env"]: expected["base_url"],
                },
            )
            self.assertNotIn("api_key", result["credential"])
            self.assertNotIn(expected["api_key"], json.dumps(result))

        if os.name != "nt":
            self.assertEqual(self.pool.secret_path.stat().st_mode & 0o777, 0o600)
        redacted = json.dumps(self.pool.list_redacted())
        for pair in self.pairs:
            self.assertNotIn(pair["api_key"], redacted)

        self.store.close()
        recovered_store, recovered_commands = build_service(self.root)
        self.store = recovered_store
        self.commands = recovered_commands
        self.pool = CredentialPoolService(self.store)
        self.pool.recover()
        resolved = self.pool.resolve_for_instance("t_credentials", "i_credential_1")
        self.assertEqual(resolved.credential_pair_id, "cred_test_01")

    def test_precommit_crash_does_not_consume_but_postcommit_crash_does(self) -> None:
        def crash_before(checkpoint: str) -> None:
            if checkpoint == "after_creation_intent":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.pool.create_instance(
                "t_credentials",
                initial_instance("t_credentials", "i_precommit"),
                provider="fake",
                creation_id="creation_precommit",
                actor=self.actor,
                crash_hook=crash_before,
            )
        first = self._create(1)
        self.assertEqual(first["credential"]["credential_pair_id"], "cred_test_01")
        resumed = self.pool.create_instance(
            "t_credentials",
            initial_instance("t_credentials", "i_precommit"),
            provider="fake",
            creation_id="creation_precommit",
            actor=self.actor,
        )
        self.assertEqual(resumed["credential"]["credential_pair_id"], "cred_test_02")

        def crash_after(checkpoint: str) -> None:
            if checkpoint == "after_assignment_event":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.pool.create_instance(
                "t_credentials",
                initial_instance("t_credentials", "i_postcommit"),
                provider="fake",
                creation_id="creation_postcommit",
                actor=self.actor,
                crash_hook=crash_after,
            )
        self.assertIsNone(self.store.instance.get("t_credentials", "i_postcommit"))
        self.pool.recover()
        recovered = self.pool.resolve_for_instance("t_credentials", "i_postcommit")
        self.assertEqual(recovered.credential_pair_id, "cred_test_03")
        fourth = self._create(4)
        self.assertEqual(fourth["credential"]["credential_pair_id"], "cred_test_01")

    def test_parallel_creation_sequence_is_deterministic_and_unique(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(self._create, range(1, 9)))
        events = [
            item
            for item in recover_records(self.pool.events_path)
            if item["event_type"] == "CREDENTIAL_PAIR_ASSIGNED"
        ]
        self.assertEqual(len(events), 8)
        self.assertEqual(
            [item["credential_pair_ref"] for item in events],
            [
                "cred_test_01",
                "cred_test_02",
                "cred_test_03",
                "cred_test_01",
                "cred_test_02",
                "cred_test_03",
                "cred_test_01",
                "cred_test_02",
            ],
        )
        self.assertEqual(len({item["creation_id"] for item in events}), 8)

    def test_full_pair_reassignment_does_not_advance_natural_cursor(self) -> None:
        first = self._create(1)
        self.assertEqual(first["credential"]["credential_pair_id"], "cred_test_01")
        reassigned = self.pool.reassign_instance(
            "t_credentials",
            "i_credential_1",
            credential_pair_id="cred_test_03",
            credential_pair_revision=1,
            idempotency_key="reassign-one",
            actor=self.actor,
        )
        self.assertEqual(reassigned["credential_pair_id"], "cred_test_03")
        self.assertEqual(
            self.pool.resolve_for_instance("t_credentials", "i_credential_1").base_url,
            "https://provider-3.invalid/v1",
        )
        second = self._create(2)
        self.assertEqual(second["credential"]["credential_pair_id"], "cred_test_02")

        replay = self.pool.reassign_instance(
            "t_credentials",
            "i_credential_1",
            credential_pair_id="cred_test_03",
            credential_pair_revision=1,
            idempotency_key="reassign-one",
            actor=self.actor,
        )
        self.assertEqual(replay, reassigned)

    def test_recovery_folds_a_complete_reassignment_chain(self) -> None:
        self._create(1)
        self.pool.reassign_instance(
            "t_credentials",
            "i_credential_1",
            credential_pair_id="cred_test_02",
            credential_pair_revision=1,
            idempotency_key="reassign-recover-two",
            actor=self.actor,
        )
        self.pool.reassign_instance(
            "t_credentials",
            "i_credential_1",
            credential_pair_id="cred_test_03",
            credential_pair_revision=1,
            idempotency_key="reassign-recover-three",
            actor=self.actor,
        )

        self.store.close()
        self.store, self.commands = build_service(self.root)
        self.pool = CredentialPoolService(self.store)
        recovered = self.pool.recover()

        self.assertEqual(len(recovered), 1)
        resolved = self.pool.resolve_for_instance("t_credentials", "i_credential_1")
        self.assertEqual(resolved.credential_pair_id, "cred_test_03")
        self.assertEqual(resolved.base_url, "https://provider-3.invalid/v1")

        replayed_creation = self._create(1)
        self.assertEqual(replayed_creation["credential"]["credential_pair_id"], "cred_test_01")
        replayed_old_reassignment = self.pool.reassign_instance(
            "t_credentials",
            "i_credential_1",
            credential_pair_id="cred_test_02",
            credential_pair_revision=1,
            idempotency_key="reassign-recover-two",
            actor=self.actor,
        )
        self.assertEqual(replayed_old_reassignment["credential_pair_id"], "cred_test_02")
        still_final = self.pool.resolve_for_instance("t_credentials", "i_credential_1")
        self.assertEqual(still_final.credential_pair_id, "cred_test_03")

    def test_old_revision_remains_resolvable_and_secrets_do_not_escape(self) -> None:
        self._create(1)
        revised = deepcopy(self.pairs)
        revised[0] = {
            **revised[0],
            "api_key": "not-a-secret-p1-01-revised",
            "base_url": "https://provider-1-revised.invalid/v1",
            "revision": 2,
        }
        self.pool.configure_pool(revised)
        pinned = self.pool.resolve_for_instance("t_credentials", "i_credential_1")
        self.assertEqual(pinned.revision, 1)
        self.assertEqual(pinned.base_url, "https://provider-1.invalid/v1")

        searchable = [self.pool.events_path, self.pool.state_path]
        searchable.extend(
            path
            for path in self.store.layout.control_root.rglob("*")
            if path.is_file() and self.pool.secret_path != path
        )
        searchable.extend(
            path
            for path in self.store.layout.workspace_root.rglob("*")
            if path.is_file()
        )
        for path in set(searchable):
            content = path.read_text(encoding="utf-8", errors="ignore")
            for pair in revised:
                self.assertNotIn(pair["api_key"], content, path)

    def test_same_revision_cannot_silently_change_one_half_of_pair(self) -> None:
        invalid = deepcopy(self.pairs)
        invalid[0]["base_url"] = "https://wrong.invalid/v1"
        with self.assertRaises(HarnessError) as captured:
            self.pool.configure_pool(invalid)
        self.assertEqual(captured.exception.code, "CREDENTIAL_PAIR_INVALID")

    def test_integrity_hmac_detects_out_of_band_secret_tampering(self) -> None:
        self._create(1)
        document = yaml.safe_load(self.pool.secret_path.read_text(encoding="utf-8"))
        document["pairs"][0]["api_key"] = "out-of-band-replacement"
        self.pool.secret_path.write_text(
            yaml.safe_dump(document, sort_keys=True), encoding="utf-8"
        )
        self.pool.secret_path.chmod(0o600)
        with self.assertRaises(HarnessError) as captured:
            self.pool.resolve_for_instance("t_credentials", "i_credential_1")
        self.assertEqual(captured.exception.code, "CREDENTIAL_PAIR_INVALID")

    def test_recovery_rejects_a_conflicting_instance_projection(self) -> None:
        self._create(1)
        instance = self.store.instance.get("t_credentials", "i_credential_1")
        instance.update(
            {"credential_pair_ref": "cred_02", "credential_pair_revision": 1}
        )
        self.store.instance.put(
            "t_credentials",
            "i_credential_1",
            instance,
            expected_revision=self.store.instance.revision(
                "t_credentials", "i_credential_1"
            ),
            actor=Actor("system", "tamper_test"),
            command="tamper_projection",
            idempotency_key="tamper-projection",
        )
        with self.assertRaises(HarnessError) as captured:
            self.pool.recover()
        self.assertEqual(captured.exception.code, "CREDENTIAL_PAIR_INVALID")

    def test_malformed_secret_document_fails_with_a_stable_error(self) -> None:
        self.pool.secret_path.write_text("active: [unterminated", encoding="utf-8")
        self.pool.secret_path.chmod(0o600)
        with self.assertRaises(HarnessError) as captured:
            self.pool.list_redacted()
        self.assertEqual(captured.exception.code, "CREDENTIAL_PAIR_INVALID")


if __name__ == "__main__":
    unittest.main()
